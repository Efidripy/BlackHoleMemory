"""Run the SQLite outbox -> Qdrant projection worker explicitly.

The command is intentionally opt-in.  ``--dry-run`` is read-only; live
projection requires ``BHM_MEMORY_STORE_MODE=sqlite-shadow`` and
``BHM_PROJECTION_WORKER_ENABLED=true`` (or an explicit ``--force``).
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parents[1]
INFRASTRUCTURE_UNAVAILABLE_EXIT_CODE = 75
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-dir", type=Path, default=None)
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--once", action="store_true", help="run one bounded batch (default)")
    parser.add_argument("--loop", action="store_true", help="poll until interrupted")
    parser.add_argument("--max-cycles", type=int, default=None)
    parser.add_argument(
        "--quiet-idle",
        action="store_true",
        help="suppress the success JSON when no outbox row was claimed",
    )
    parser.add_argument(
        "--openai-base-url",
        default=None,
        help=(
            "process-local provider endpoint override (never persisted); "
            "must be an http(s) URL without credentials, query or fragment"
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="run despite a disabled worker flag; mode and path guards still apply",
    )
    return parser


def _validate_openai_base_url(value: str) -> str:
    """Validate and normalize a process-local provider endpoint override."""

    candidate = str(value or "").strip().rstrip("/")
    parsed = urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("--openai-base-url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError("--openai-base-url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ValueError("--openai-base-url must not contain a query or fragment")
    if any(character.isspace() for character in candidate):
        raise ValueError("--openai-base-url must not contain whitespace")
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("--openai-base-url contains an invalid port") from exc
    try:
        from blackholememory.local_endpoint_policy import validate_local_endpoint

        return validate_local_endpoint(candidate)
    except Exception as exc:
        raise ValueError("--openai-base-url must target a local-only provider endpoint") from exc


def _apply_provider_override(value: str | None) -> str | None:
    """Apply an explicit provider URL only to this worker process."""

    if value is None:
        return None
    normalized = _validate_openai_base_url(value)
    os.environ["OPENAI_BASE_URL"] = normalized
    return normalized


def _read_only_outbox_summary(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"database_exists": False, "outbox": {}}
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM memory_outbox GROUP BY status ORDER BY status"
        ).fetchall()
        return {
            "database_exists": True,
            "outbox": {str(status): int(count) for status, count in rows},
        }
    except sqlite3.DatabaseError as exc:
        return {"database_exists": True, "outbox": {}, "error": str(exc)[:500]}
    finally:
        connection.close()


def _build_worker(config, *, openai_base_url: str | None = None):
    from qdrant_client.http import models as qdrant_models

    from blackholememory.config import settings
    from blackholememory.memory_repository import SQLiteMemoryRepository
    from blackholememory.mem0_adapter import get_project_mem0_memory
    from blackholememory.mem0_adapter import get_qdrant_client
    from blackholememory.projection_worker import ProjectionWorker
    from blackholememory.qdrant_projector import QdrantProjector

    if openai_base_url is not None:
        # ``settings`` may already be imported by a caller; bind both the
        # process-local environment and the in-process settings object so the
        # override cannot be bypassed by import order.  No file is changed.
        settings.mem0_openai_base_url = openai_base_url

    repository = SQLiteMemoryRepository(config.database_path)
    repository.initialize()
    client = get_qdrant_client()
    embedding_models: dict[str, Any] = {}

    def vectorizer(memory):
        model = embedding_models.get(memory.project)
        if model is None:
            model = get_project_mem0_memory(memory.project).embedding_model
            embedding_models[memory.project] = model
        return model.embed(memory.current_revision.content, "add")

    def ensure_collection(collection_name: str) -> None:
        if client.collection_exists(collection_name):
            return
        try:
            client.create_collection(
                collection_name=collection_name,
                vectors_config=qdrant_models.VectorParams(
                    size=settings.mem0_embedding_dims,
                    distance=qdrant_models.Distance.COSINE,
                ),
            )
        except Exception:
            if not client.collection_exists(collection_name):
                raise

    projector = QdrantProjector(
        client,
        vectorizer,
        expected_dimensions=settings.mem0_embedding_dims,
        ensure_collection=ensure_collection,
    )
    return ProjectionWorker(repository, projector, config=config.projection_worker)


def _worker_report(config, metrics) -> dict[str, Any]:
    return {
        "ok": metrics.last_error is None,
        "writes_live_state": False,
        "mode": config.mode.value,
        "database_path": str(config.database_path),
        "metrics": metrics.as_dict(),
    }


def _emit_worker_report(report: dict[str, Any], *, quiet_idle: bool) -> int:
    metrics = dict(report.get("metrics") or {})
    classification = str(metrics.get("last_classification") or "")
    if classification == "infrastructure_unavailable":
        diagnostic = {
            "timestamp": metrics.get("last_run_at"),
            "classification": classification,
            "deferred": int(metrics.get("deferred") or 0),
            "error": str(metrics.get("last_error") or "projection infrastructure unavailable")[:2_000],
        }
        print(json.dumps(diagnostic, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return INFRASTRUCTURE_UNAVAILABLE_EXIT_CODE
    if quiet_idle and int(metrics.get("claimed") or 0) == 0 and not metrics.get("last_error"):
        return 0
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report.get("ok") else 1


def main() -> int:
    args = _parser().parse_args()
    if args.once and args.loop:
        print("--once and --loop are mutually exclusive", file=sys.stderr)
        return 2
    if args.max_cycles is not None and args.max_cycles < 1:
        print("--max-cycles must be positive", file=sys.stderr)
        return 2

    from blackholememory.runtime_storage import MemoryStoreMode
    from blackholememory.runtime_storage import evaluate_runtime_storage_state
    from blackholememory.runtime_storage import inspect_memory_store_schema
    from blackholememory.runtime_storage import resolve_runtime_storage_config

    config = resolve_runtime_storage_config(runtime_dir=args.runtime_dir)
    if args.database is not None:
        database = args.database.expanduser()
        if not database.is_absolute():
            database = (config.database_path.parent / database).resolve()
        config = replace(config, database_path=database.resolve())
    database_ready, _ = inspect_memory_store_schema(config.database_path)
    state = evaluate_runtime_storage_state(config, database_ready=database_ready)

    if args.dry_run:
        report = {
            "ok": True,
            "writes_live_state": False,
            "config": config.as_dict(),
            "state": state.as_dict(),
            "outbox": _read_only_outbox_summary(config.database_path),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if config.mode is not MemoryStoreMode.SQLITE_SHADOW:
        print(
            "projection worker is restricted to sqlite-shadow mode; "
            f"configured={config.mode.value}",
            file=sys.stderr,
        )
        return 2
    if not config.database_path.exists():
        print(f"SQLite database does not exist: {config.database_path}", file=sys.stderr)
        return 2
    if not state.database_schema_ready:
        print(
            "SQLite database schema is not valid; run a reviewed migration first: "
            f"{config.database_path}",
            file=sys.stderr,
        )
        return 2
    if not config.projection_worker.enabled and not args.force:
        print(
            "projection worker is disabled; set BHM_PROJECTION_WORKER_ENABLED=true or pass --force",
            file=sys.stderr,
        )
        return 2

    try:
        provider_override = _apply_provider_override(args.openai_base_url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    worker = _build_worker(config, openai_base_url=provider_override)
    try:
        if args.loop:
            metrics = worker.run_forever(max_cycles=args.max_cycles, force=args.force)
        else:
            worker.run_once(force=args.force)
            metrics = worker.snapshot()
    except KeyboardInterrupt:
        worker.stop()
        metrics = worker.snapshot()
    except Exception as exc:
        print(f"projection worker failed: {exc}", file=sys.stderr)
        return 1

    return _emit_worker_report(
        _worker_report(config, metrics),
        quiet_idle=args.quiet_idle,
    )


if __name__ == "__main__":
    raise SystemExit(main())
