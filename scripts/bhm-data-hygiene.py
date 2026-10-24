"""Fail-closed operator CLI for the two-phase BHM data-hygiene workflow."""

from __future__ import annotations

import argparse
import json
import socket
import sys
from pathlib import Path
from typing import Any, Iterable

import psutil


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

# ruff: noqa: E402
from blackholememory.data_hygiene import DataHygieneError
from blackholememory.data_hygiene import load_data_hygiene_policy
from blackholememory.data_hygiene import plan_data_hygiene
from blackholememory.data_hygiene import prepare_data_hygiene
from blackholememory.data_hygiene import purge_data_hygiene
from blackholememory.data_hygiene import restore_data_hygiene
from blackholememory.data_hygiene import verify_projection_absence
from blackholememory.local_endpoint_policy import validate_local_endpoint
from blackholememory.runtime_endpoints import endpoint_parts


DEFAULT_DATABASE = REPO_ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
DEFAULT_POLICY = REPO_ROOT / "config" / "data-hygiene-policy.json"
ARTIFACT_ROOT = REPO_ROOT / ".runtime" / "data-hygiene"
PROJECTION_PID_PATH = REPO_ROOT / ".runtime" / "bootstrap" / "projection-sidecar.pid"
PROJECTION_PROOF_SCHEMA = "bhm.data-hygiene-projection-proof.v1"


def _path(value: str) -> Path:
    return Path(value).expanduser()


def _require_file(path: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if not resolved.is_file():
        raise ValueError(f"{label} must be a regular file: {resolved}")
    if path.is_symlink():
        raise ValueError(f"{label} must not be a symlink: {resolved}")
    return resolved


def _require_runtime_artifact_path(path: Path, *, label: str, must_exist: bool) -> Path:
    resolved = path.resolve(strict=must_exist)
    root = ARTIFACT_ROOT.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} must stay under {root}") from exc
    if must_exist:
        return _require_file(resolved, label=label)
    if resolved.exists():
        raise ValueError(f"{label} already exists: {resolved}")
    return resolved


def _normalize_ids(values: Iterable[Any], *, source: str) -> set[str]:
    result: set[str] = set()
    for raw in values:
        if not isinstance(raw, str):
            raise ValueError(f"{source} must contain only string memory IDs")
        value = raw.strip()
        if not value or value != raw or len(value) > 512:
            raise ValueError(f"{source} contains an invalid memory ID")
        result.add(value)
    if not result:
        raise ValueError(f"{source} contains no memory IDs")
    return result


def _load_projection_absence_report(path: Path) -> set[str]:
    report_path = _require_runtime_artifact_path(path, label="projection absence report", must_exist=True)
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("projection absence report must be a JSON object")
    if payload.get("schema_version") != PROJECTION_PROOF_SCHEMA:
        raise ValueError("projection absence report has an unsupported schema")
    if payload.get("complete") is not True or payload.get("present") or payload.get("errors"):
        raise ValueError("projection absence proof is incomplete")
    absent = payload.get("absent_ids")
    if not isinstance(absent, list):
        raise ValueError("projection absence report must contain absent_ids array")
    normalized = _normalize_ids(absent, source="projection absence report.absent_ids")
    if len(normalized) != len(absent) or payload.get("candidate_count") != len(normalized):
        raise ValueError("projection absence report candidate count is inconsistent")
    proof_digest = str(payload.get("proof_digest") or "")
    if len(proof_digest) != 64 or any(char not in "0123456789abcdef" for char in proof_digest):
        raise ValueError("projection absence report proof digest is invalid")
    return normalized


def _write_result(result: Any, output: Path | None) -> None:
    rendered = json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True, default=str) + "\n"
    if output is None:
        sys.stdout.write(rendered)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8", newline="\n")
    sys.stdout.write(rendered)


def _require_offline_confirmation(args: argparse.Namespace) -> None:
    if not bool(args.confirm_offline):
        raise ValueError(f"{args.command} requires --confirm-offline")


def _require_runtime_offline() -> None:
    host, port = endpoint_parts("bhm_api")
    try:
        with socket.create_connection((host, port), timeout=0.25):
            raise ValueError(f"BHM API is still listening on {host}:{port}")
    except (ConnectionRefusedError, TimeoutError, socket.timeout, OSError):
        pass
    if not PROJECTION_PID_PATH.is_file():
        return
    try:
        pid = int(PROJECTION_PID_PATH.read_text(encoding="utf-8").strip())
        process = psutil.Process(pid)
        command = " ".join(process.cmdline()).casefold()
    except (psutil.NoSuchProcess, ValueError):
        return
    except psutil.AccessDenied as exc:
        raise ValueError(f"cannot prove projection sidecar PID {pid} is stopped") from exc
    if "run-bhm-projection-sidecar.ps1" in command:
        raise ValueError(f"projection sidecar is still running with PID {pid}")


def _require_distinct_backup(database: Path, backup: Path) -> None:
    if database == backup:
        raise ValueError("existing full backup must be distinct from the authoritative database")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=_path, default=DEFAULT_DATABASE)
    parser.add_argument("--policy", type=_path, default=DEFAULT_POLICY)
    parser.add_argument("--output", type=_path, help="immutable JSON receipt below .runtime/data-hygiene")
    subparsers = parser.add_subparsers(dest="command", required=True)

    plan = subparsers.add_parser("plan", help="build a read-only exact-scope cleanup plan")
    plan.add_argument("--existing-backup", type=_path, required=True)
    plan.add_argument("--as-of", help="stable ISO-8601 planning time; defaults to the API clock")

    prepare = subparsers.add_parser("prepare", help="offline retire phase and rollback-package creation")
    prepare.add_argument("--existing-backup", type=_path, required=True)
    prepare.add_argument("--rollback-package", type=_path, required=True)
    prepare.add_argument("--expected-plan-digest", required=True)
    prepare.add_argument("--as-of", required=True, help="exact as_of value from plan")
    prepare.add_argument("--confirm-offline", action="store_true")

    projection = subparsers.add_parser(
        "projection-check",
        help="verify every reviewed memory is absent from Qdrant projection",
    )
    projection.add_argument("--qdrant-url", default="http://127.0.0.1:6333")

    purge = subparsers.add_parser("purge", help="offline purge after projection drain and absence proof")
    purge.add_argument("--existing-backup", type=_path, required=True)
    purge.add_argument("--expected-plan-digest", required=True)
    purge.add_argument("--as-of", required=True, help="exact as_of value from the post-prepare plan")
    purge.add_argument("--projection-absence-report", type=_path, required=True)
    purge.add_argument("--confirm-offline", action="store_true")

    restore = subparsers.add_parser("restore", help="offline selective restore from rollback package")
    restore.add_argument("--rollback-package", type=_path, required=True)
    restore.add_argument("--confirm-offline", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        exit_code = 0
        database = _require_file(args.database, label="authoritative database")
        policy = load_data_hygiene_policy(_require_file(args.policy, label="data-hygiene policy"))
        output = (
            _require_runtime_artifact_path(args.output, label="output receipt", must_exist=False)
            if args.output is not None
            else None
        )

        if args.command == "plan":
            backup = _require_file(args.existing_backup, label="existing full backup")
            _require_distinct_backup(database, backup)
            result = plan_data_hygiene(database, policy, backup, as_of=args.as_of)
        elif args.command == "prepare":
            _require_offline_confirmation(args)
            _require_runtime_offline()
            backup = _require_file(args.existing_backup, label="existing full backup")
            _require_distinct_backup(database, backup)
            package = _require_runtime_artifact_path(args.rollback_package, label="rollback package", must_exist=False)
            if output == package:
                raise ValueError("output receipt and rollback package must be distinct")
            package.parent.mkdir(parents=True, exist_ok=True)
            result = prepare_data_hygiene(
                database,
                policy,
                backup,
                package,
                expected_plan_digest=args.expected_plan_digest,
                as_of=args.as_of,
                offline=True,
            )
        elif args.command == "projection-check":
            if output is None:
                raise ValueError("projection-check requires --output below .runtime/data-hygiene")
            result = verify_projection_absence(
                database,
                policy,
                qdrant_url=validate_local_endpoint(args.qdrant_url),
            )
            if result.get("complete") is not True:
                exit_code = 3
        elif args.command == "purge":
            _require_offline_confirmation(args)
            _require_runtime_offline()
            backup = _require_file(args.existing_backup, label="existing full backup")
            _require_distinct_backup(database, backup)
            proof_ids = _load_projection_absence_report(args.projection_absence_report)
            result = purge_data_hygiene(
                database,
                policy,
                backup,
                expected_plan_digest=args.expected_plan_digest,
                as_of=args.as_of,
                offline=True,
                projection_absent_ids=proof_ids,
            )
        else:
            _require_offline_confirmation(args)
            _require_runtime_offline()
            package = _require_runtime_artifact_path(args.rollback_package, label="rollback package", must_exist=True)
            result = restore_data_hygiene(database, package, offline=True)
        _write_result(result, output)
        return exit_code
    except (DataHygieneError, OSError, ValueError, json.JSONDecodeError) as exc:
        parser.exit(2, f"data-hygiene {args.command} failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
