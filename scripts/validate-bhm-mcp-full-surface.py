#!/usr/bin/env python
"""Run the complete BHM MCP catalog against disposable, repository-scoped fixtures.

This validator treats the target repository as read-only. BHM may update its own
authoritative SQLite state and runtime graph artifacts. Every MCP invocation is
written as a unique monotonic receipt, including prerequisite and cleanup calls.
Only calls marked ``counts_toward_catalog`` participate in the 1..186 coverage
verdict.
"""

# ruff: noqa: E402

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import httpx


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
SCRIPT_ROOT = REPO_ROOT / "scripts"
for import_root in (SRC_ROOT, SCRIPT_ROOT):
    if str(import_root) not in sys.path:
        sys.path.insert(0, str(import_root))

from blackholememory.caller_auth import configured_caller_token
from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.mcp_surfaces import CORE_TOOL_NAMES
from blackholememory.project_retirement import PROJECT_RETIREMENT_ALLOWLIST_ENV
from blackholememory.project_retirement import PROJECT_RETIREMENT_CAPABILITY_ENV
from blackholememory.runtime_endpoints import endpoint_url


SCHEMA_VERSION = "bhm.mcp.full-surface-validation.v1"
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_INDEX_TIMEOUT_SECONDS = 900.0
CATALOG_FIRST = 1
CATALOG_LAST = 186
_SAFE_RECEIPT_PART = re.compile(r"[^a-z0-9-]+")
_PROJECT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    try:
        return value.model_dump(mode="json")
    except Exception:
        return str(value)


def _redact(value: Any, key: str = "") -> Any:
    lowered = key.casefold()
    if any(
        word in lowered
        for word in ("token", "capability", "secret", "password", "credential")
    ):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): _redact(item, str(item_key))
            for item_key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item, key) for item in value]
    return _json_safe(value)


def _safe_receipt_part(value: str) -> str:
    normalized = _SAFE_RECEIPT_PART.sub(
        "-", str(value or "call").strip().casefold()
    ).strip("-")
    return normalized[:80] or "call"


def _write_json(path: Path, payload: Any) -> None:
    encoded = (
        json.dumps(_json_safe(payload), ensure_ascii=False, indent=2, sort_keys=True)
        + "\n"
    ).encode("utf-8")
    replace_bytes_safely(path, encoded)


def _git(repository: Path, command: list[str]) -> str:
    return subprocess.check_output(
        ["git", "-C", str(repository), *command],
        text=True,
        encoding="utf-8",
        errors="replace",
    ).strip()


def repository_snapshot(repository: Path) -> dict[str, Any]:
    tracked = _git(repository, ["ls-files", "-co", "--exclude-standard"]).splitlines()
    digest = hashlib.sha256()
    files: list[dict[str, Any]] = []
    for relative in sorted(set(tracked)):
        path = repository / relative
        if not path.is_file():
            continue
        data = path.read_bytes()
        normalized = relative.replace("\\", "/")
        file_digest = hashlib.sha256(data).hexdigest()
        digest.update(normalized.encode("utf-8"))
        digest.update(b"\0")
        digest.update(bytes.fromhex(file_digest))
        files.append({"path": normalized, "sha256": file_digest, "bytes": len(data)})
    return {
        "head": _git(repository, ["rev-parse", "HEAD"]),
        "branch": _git(repository, ["branch", "--show-current"]),
        "status": _git(repository, ["status", "--porcelain=v1"]),
        "content_digest": digest.hexdigest(),
        "files": files,
    }


def source_refs(repository: Path, snapshot: Mapping[str, Any]) -> list[str]:
    paths = [
        str(item.get("path") or "")
        for item in snapshot.get("files", [])
        if isinstance(item, Mapping) and item.get("path")
    ]
    preferred: list[str] = []
    for candidate in paths:
        lowered = candidate.casefold()
        if lowered == "readme.md" or lowered.endswith(
            (".sln", ".slnx", "appsettings.json")
        ):
            preferred.append(candidate)
    for candidate in paths:
        if candidate not in preferred:
            preferred.append(candidate)
        if len(preferred) >= 3:
            break
    return [str((repository / relative).resolve()) for relative in preferred[:3]]


def parse_result(result: Any) -> tuple[Any, bool, str]:
    try:
        dumped = result.model_dump(mode="json")
    except Exception:
        dumped = result
    is_error = (
        bool(dumped.get("isError", dumped.get("is_error", False)))
        if isinstance(dumped, Mapping)
        else False
    )
    structured = (
        dumped.get("structuredContent", dumped.get("structured_content"))
        if isinstance(dumped, Mapping)
        else None
    )
    if structured is None and isinstance(dumped, Mapping):
        for item in dumped.get("content") or []:
            text = item.get("text") if isinstance(item, Mapping) else None
            if not text:
                continue
            try:
                structured = json.loads(text)
            except Exception:
                structured = {"text": text}
            break
    if structured is None:
        structured = dumped
    safe_dumped = _json_safe(dumped)
    return (
        _json_safe(structured),
        is_error,
        json.dumps(safe_dumped, ensure_ascii=False, default=str)[:2000],
    )


def catalog_receipt(names: list[str]) -> dict[str, Any]:
    expected_names = set(CORE_TOOL_NAMES)
    missing_core = sorted(expected_names - set(names))
    duplicate_names = sorted(name for name, count in Counter(names).items() if count > 1)
    return {
        "count": len(names),
        "unique": len(set(names)),
        "expected_catalog_count": CATALOG_LAST,
        "expected_core_count": len(expected_names),
        "missing_core": missing_core,
        "duplicate_names": duplicate_names,
        "core_present": not missing_core,
        "catalog_count_matches": len(names) == CATALOG_LAST,
        "contract_ok": (
            len(names) == CATALOG_LAST
            and len(set(names)) == CATALOG_LAST
            and not missing_core
        ),
        "names": names,
    }


class Runner:
    """Record every call once while keeping catalog coverage explicit."""

    def __init__(
        self,
        module: Any,
        run_dir: Path,
        state: dict[str, Any],
        *,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        index_timeout_seconds: float = DEFAULT_INDEX_TIMEOUT_SECONDS,
    ) -> None:
        self.module = module
        self.run_dir = run_dir
        self.state = state
        self.timeout_seconds = float(timeout_seconds)
        self.index_timeout_seconds = float(index_timeout_seconds)
        self.results: list[dict[str, Any]] = []
        self.call_count = 0
        self.receipt_dir = run_dir / "receipts"
        self.receipt_dir.mkdir(parents=True, exist_ok=True)

    async def call(
        self,
        number: int | None,
        name: str,
        args: dict[str, Any] | None = None,
        *,
        timeout: float | None = None,
        expected_rejection: bool = False,
        expected_empty: bool = False,
        note: str = "",
        validator: Callable[[Any], tuple[bool, str]] | None = None,
        counts_toward_catalog: bool = True,
        stage: str = "catalog",
    ) -> Any:
        if counts_toward_catalog and (
            number is None or not CATALOG_FIRST <= int(number) <= CATALOG_LAST
        ):
            raise ValueError("catalog calls require a number between 1 and 186")
        args = args or {}
        self.call_count += 1
        call_id = self.call_count
        started = time.perf_counter()
        item: dict[str, Any] = {
            "call_id": call_id,
            "catalog_number": int(number) if number is not None else None,
            "counts_toward_catalog": bool(counts_toward_catalog),
            "stage": stage,
            "name": name,
            "args": _redact(args),
            "started_at": utc_now(),
            "status": "FAIL",
            "note": note,
        }
        payload: Any = None
        effective_timeout = timeout
        if effective_timeout is None:
            effective_timeout = (
                self.index_timeout_seconds
                if number in {163, 166}
                else self.timeout_seconds
            )
        try:
            result = await asyncio.wait_for(
                self.module.mcp.call_tool(name, args), timeout=effective_timeout
            )
            payload, is_error, raw = parse_result(result)
            item["response_preview"] = raw
            item["response"] = payload
            if is_error:
                item["status"] = (
                    "PASS_EXPECTED_REJECTION" if expected_rejection else "FAIL"
                )
                item["reason"] = "tool_result_is_error"
            elif validator is not None:
                ok, reason = validator(payload)
                item["status"] = "PASS" if ok else "FAIL"
                item["reason"] = reason
            else:
                item["status"] = "PASS_EXPECTED_EMPTY" if expected_empty else "PASS"
                item["reason"] = "call_completed"
        except Exception as exc:
            item["error"] = f"{type(exc).__name__}: {exc}"[:4000]
            item["status"] = "PASS_EXPECTED_REJECTION" if expected_rejection else "FAIL"
            item["reason"] = (
                "expected_rejection" if expected_rejection else "exception_or_timeout"
            )
        item["duration_ms"] = round((time.perf_counter() - started) * 1000, 2)
        item["finished_at"] = utc_now()
        catalog_number = int(number) if number is not None else 0
        filename = (
            f"{call_id:04d}-{catalog_number:03d}-{_safe_receipt_part(stage)}-"
            f"{_safe_receipt_part(name)}.json"
        )
        item["receipt_path"] = str(Path("receipts") / filename).replace("\\", "/")
        self.results.append(item)
        _write_json(self.receipt_dir / filename, item)
        if item["status"] == "FAIL":
            print(
                f"FAIL call={call_id:04d} catalog={catalog_number:03d} {name}",
                flush=True,
            )
        elif call_id % 10 == 0:
            print(f"progress calls={call_id} last={name}", flush=True)
        return payload

    def aggregate(self) -> dict[str, Any]:
        by_number: dict[int, list[dict[str, Any]]] = {}
        prerequisites = [
            item for item in self.results if not item["counts_toward_catalog"]
        ]
        for item in self.results:
            if not item["counts_toward_catalog"]:
                continue
            number = int(item["catalog_number"])
            by_number.setdefault(number, []).append(item)
        rows: list[dict[str, Any]] = []
        for number in sorted(by_number):
            attempts = by_number[number]
            failed = [attempt for attempt in attempts if attempt["status"] == "FAIL"]
            rows.append(
                {
                    "number": number,
                    "name": attempts[-1]["name"],
                    "status": "NOT OK" if failed else "OK",
                    "attempts": len(attempts),
                    "call_ids": [int(attempt["call_id"]) for attempt in attempts],
                    "reason": (failed[-1].get("error") or failed[-1].get("reason"))
                    if failed
                    else attempts[-1].get("reason", ""),
                }
            )
        expected = set(range(CATALOG_FIRST, CATALOG_LAST + 1))
        missing = sorted(expected - set(by_number))
        catalog_failures = [row for row in rows if row["status"] == "NOT OK"]
        prerequisite_failures = [
            item for item in prerequisites if item["status"] == "FAIL"
        ]
        return {
            "rows": rows,
            "missing_numbers": missing,
            "catalog_calls": sum(len(items) for items in by_number.values()),
            "prerequisite_calls": len(prerequisites),
            "total_calls": len(self.results),
            "prerequisite_failures": [
                int(item["call_id"]) for item in prerequisite_failures
            ],
            "ok": not missing and not catalog_failures and not prerequisite_failures,
        }


async def public_ready_probe(base_url: str, timeout_seconds: float) -> dict[str, Any]:
    try:
        async with httpx.AsyncClient(
            timeout=timeout_seconds, follow_redirects=False, trust_env=False
        ) as client:
            response = await client.get(f"{base_url.rstrip('/')}/health/ready")
        return {
            "transport": "public_http",
            "path": "/health/ready",
            "status_code": response.status_code,
            "ok": response.status_code == 200,
            "payload": response.json(),
        }
    except Exception as exc:
        return {
            "transport": "public_http",
            "path": "/health/ready",
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}"[:1000],
        }


async def closeout_health(
    runner: Runner,
    *,
    base_url: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    native_health = await runner.call(
        None,
        "bhm_health",
        counts_toward_catalog=False,
        stage="closeout-health-native",
        note="authenticated native MCP closeout health",
    )
    native_receipt = runner.results[-1]
    public_ready = await public_ready_probe(base_url, min(float(timeout_seconds), 30.0))
    return {
        "native_mcp": {
            "authenticated": True,
            "ok": native_receipt["status"] != "FAIL",
            "call_id": native_receipt["call_id"],
            "payload": native_health,
        },
        "public_ready": public_ready,
    }


def _retirement_action(payload: Any) -> str:
    if isinstance(payload, Mapping):
        action = payload.get("action")
        if isinstance(action, str):
            return action
        for value in payload.values():
            action = _retirement_action(value)
            if action:
                return action
    if isinstance(payload, list):
        for value in payload:
            action = _retirement_action(value)
            if action:
                return action
    return ""


def validate_fixture_policy_preflight(
    fixture_policy: str,
    *,
    projects: list[str] | None = None,
) -> None:
    if fixture_policy not in {"retain", "retire"}:
        raise ValueError("fixture_policy must be retain or retire")
    if fixture_policy != "retire":
        return
    if not os.getenv(PROJECT_RETIREMENT_CAPABILITY_ENV, "").strip():
        raise RuntimeError(
            f"{PROJECT_RETIREMENT_CAPABILITY_ENV} is required before retire-policy fixtures are created"
        )
    expected = [str(project or "").strip().casefold() for project in projects or []]
    if len(expected) != 2 or len(set(expected)) != 2:
        raise RuntimeError(
            "retire policy requires two distinct explicit fixture project ids"
        )
    configured_allowlist = {
        item.strip().casefold()
        for item in os.getenv(PROJECT_RETIREMENT_ALLOWLIST_ENV, "").split(",")
        if item.strip()
    }
    missing = sorted(set(expected) - configured_allowlist)
    if missing:
        raise RuntimeError(
            f"{PROJECT_RETIREMENT_ALLOWLIST_ENV} must include the exact fixture project ids before creation: "
            + ",".join(missing)
        )


def resolve_fixture_projects(args: argparse.Namespace, run_id: str) -> list[str]:
    explicit = [
        str(getattr(args, "fixture_project", "") or "").strip().casefold(),
        str(getattr(args, "fixture_peer_project", "") or "").strip().casefold(),
    ]
    if args.fixture_policy == "retire" and not all(explicit):
        raise RuntimeError(
            "retire policy requires --fixture-project and --fixture-peer-project so the server allowlist can be configured before creation"
        )
    if all(explicit):
        projects = explicit
    else:
        base = _safe_receipt_part(str(args.repository_project))[:64]
        stamp = run_id.casefold()
        projects = [
            f"{base}-bhm-conformance-{stamp}",
            f"{base}-bhm-conformance-peer-{stamp}",
        ]
    if len(set(projects)) != 2 or any(
        not _PROJECT_ID.fullmatch(project) for project in projects
    ):
        raise ValueError("fixture project ids must be distinct canonical lowercase ids")
    return projects


async def close_fixture_lifecycle(
    runner: Runner,
    state: dict[str, Any],
    *,
    fixture_policy: str,
) -> dict[str, Any]:
    projects = [str(state["project"]), str(state["peer_project"])]
    if fixture_policy == "retain":
        return {
            "schema_version": SCHEMA_VERSION,
            "policy": "retain",
            "ok": True,
            "status": "retained",
            "cleanup_attempted": False,
            "projects": [
                {"project": project, "status": "retained"} for project in projects
            ],
        }

    capability = os.getenv(PROJECT_RETIREMENT_CAPABILITY_ENV, "").strip()
    if not capability:
        return {
            "schema_version": SCHEMA_VERSION,
            "policy": "retire",
            "ok": False,
            "status": "cleanup_incomplete",
            "cleanup_attempted": False,
            "reason": f"{PROJECT_RETIREMENT_CAPABILITY_ENV} is unavailable",
            "retry_projects": projects,
        }

    rows: list[dict[str, Any]] = []
    for project in projects:
        payload = await runner.call(
            None,
            "bhm_project_retire",
            {
                "project": project,
                "apply": True,
                "capability": capability,
                "backup_dir": None,
            },
            timeout=runner.index_timeout_seconds,
            counts_toward_catalog=False,
            stage="fixture-retire",
            note="retire disposable full-surface validation project",
        )
        receipt = runner.results[-1]
        action = _retirement_action(payload)
        ok = receipt["status"] != "FAIL" and action in {
            "retired",
            "already_retired",
        }
        rows.append(
            {
                "project": project,
                "ok": ok,
                "status": action or "cleanup_failed",
                "call_id": receipt["call_id"],
                "retry": None
                if ok
                else {
                    "tool": "bhm_project_retire",
                    "project": project,
                    "apply": True,
                },
            }
        )
    ok = all(row["ok"] for row in rows) and len(rows) == len(projects)
    return {
        "schema_version": SCHEMA_VERSION,
        "policy": "retire",
        "ok": ok,
        "status": "retired" if ok else "cleanup_incomplete",
        "cleanup_attempted": True,
        "required_server_exact_allowlist": projects,
        "projects": rows,
        "retry_projects": [row["project"] for row in rows if not row["ok"]],
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repository", type=Path, required=True, help="read-only target Git repository"
    )
    parser.add_argument(
        "--repository-project",
        required=True,
        help="BHM project id for repository intelligence",
    )
    parser.add_argument(
        "--repository-root",
        required=True,
        help="allowlisted root argument passed to BHM",
    )
    parser.add_argument("--fixture-policy", choices=("retain", "retire"), required=True)
    parser.add_argument("--fixture-project", default="")
    parser.add_argument("--fixture-peer-project", default="")
    parser.add_argument("--base-url", default=endpoint_url("bhm_api"))
    parser.add_argument(
        "--output-root",
        type=Path,
        default=REPO_ROOT / ".runtime" / "validation" / "bhm-mcp-full-surface",
    )
    parser.add_argument(
        "--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS
    )
    parser.add_argument(
        "--index-timeout-seconds", type=float, default=DEFAULT_INDEX_TIMEOUT_SECONDS
    )
    return parser.parse_args()


async def run_validation(args: argparse.Namespace) -> dict[str, Any]:
    from blackholememory import bhm_mcp
    from bhm_mcp_full_surface_code import run_code_tools
    from bhm_mcp_full_surface_memory import run_memory_tools

    token = configured_caller_token()
    if len(token) < 32:
        raise RuntimeError(
            "BHM_CALLER_TOKEN is unavailable; native MCP validation requires authenticated bridge calls"
        )
    repository = args.repository.resolve()
    if not (repository / ".git").exists():
        raise ValueError("repository must be a Git checkout")
    if args.timeout_seconds <= 0 or args.index_timeout_seconds <= 0:
        raise ValueError("timeouts must be positive")

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fixture_projects = resolve_fixture_projects(args, run_id)
    validate_fixture_policy_preflight(args.fixture_policy, projects=fixture_projects)
    run_dir = args.output_root.resolve() / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    repo_before = repository_snapshot(repository)
    state: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "project": fixture_projects[0],
        "peer_project": fixture_projects[1],
        "repo_root": str(repository),
        "repo_project": args.repository_project,
        "repo_root_argument": args.repository_root,
        "fixture_policy": args.fixture_policy,
        "authenticated_native_mcp": True,
        "repo_before": repo_before,
        "source_refs": source_refs(repository, repo_before),
        "started_at": utc_now(),
    }
    catalog = await bhm_mcp.mcp.list_tools()
    names = [str(getattr(tool, "name", "")) for tool in catalog]
    state["catalog"] = catalog_receipt(names)
    _write_json(run_dir / "manifest.json", state)
    runner = Runner(
        bhm_mcp,
        run_dir,
        state,
        timeout_seconds=args.timeout_seconds,
        index_timeout_seconds=args.index_timeout_seconds,
    )
    execution_error: str | None = None
    lifecycle: dict[str, Any] = {}
    try:
        await runner.call(
            1,
            "bhm_health",
            stage="catalog-health",
            note="authenticated native MCP health",
        )
        await run_memory_tools(runner, state)
        await run_code_tools(runner, state)
    except Exception as exc:
        execution_error = f"{type(exc).__name__}: {exc}"[:4000]
    finally:
        lifecycle = await close_fixture_lifecycle(
            runner, state, fixture_policy=args.fixture_policy
        )
        _write_json(run_dir / "lifecycle.json", lifecycle)
        state["health_after"] = await closeout_health(
            runner,
            base_url=args.base_url,
            timeout_seconds=args.timeout_seconds,
        )
        state["repo_after"] = repository_snapshot(repository)
        state["repo_unchanged"] = state["repo_before"] == state["repo_after"]
        summary = runner.aggregate()
        summary["execution_error"] = execution_error
        summary["repo_unchanged"] = state["repo_unchanged"]
        summary["fixture_lifecycle_ok"] = lifecycle.get("ok") is True
        summary["catalog_contract_ok"] = state["catalog"]["contract_ok"] is True
        summary["health_closeout_ok"] = bool(
            state["health_after"]["native_mcp"]["ok"]
            and state["health_after"]["public_ready"].get("ok") is True
        )
        summary["ok"] = bool(
            summary["ok"]
            and execution_error is None
            and state["repo_unchanged"]
            and lifecycle.get("ok") is True
            and summary["catalog_contract_ok"]
            and summary["health_closeout_ok"]
        )
        state["summary"] = summary
        state["finished_at"] = utc_now()
        _write_json(run_dir / "manifest.json", state)
        _write_json(run_dir / "results.json", runner.results)
        _write_json(run_dir / "summary.json", summary)
    return {
        "run_dir": str(run_dir),
        "summary": state["summary"],
        "lifecycle": lifecycle,
    }


def main() -> int:
    report = asyncio.run(run_validation(_parse_args()))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["summary"]["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
