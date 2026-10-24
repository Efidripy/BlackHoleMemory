"""Jmaka validation phase for BHM MCP code tools 163-186.

The repository root is intentionally passed as the allowlisted relative name
``Jmaka``.  BHM may write its own SQLite index and runtime graph artifact, but
none of the calls below writes to the Jmaka worktree.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any


PROJECT = "jmaka"
ROOT = "Jmaka"
MAX_FILES_PER_RUN = 666
MAX_RESUME_SLICES = 64


def _deep_value(value: Any, key: str) -> Any:
    """Return the first recursively discovered value for *key*."""
    if isinstance(value, Mapping):
        if key in value:
            return value[key]
        for child in value.values():
            found = _deep_value(child, key)
            if found is not None:
                return found
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for child in value:
            found = _deep_value(child, key)
            if found is not None:
                return found
    return None


def _index_block(value: Any) -> Mapping[str, Any]:
    block = _deep_value(value, "index")
    return block if isinstance(block, Mapping) else {}


def _index_status(value: Any) -> str:
    block = _index_block(value)
    status = block.get("status")
    if status is None:
        status = _deep_value(value, "status")
    return str(status or "").strip().lower()


def _snapshot_id(value: Any) -> str | None:
    block = _index_block(value)
    direct = block.get("snapshot_id")
    if direct:
        return str(direct)
    snapshot = block.get("snapshot")
    if isinstance(snapshot, Mapping) and snapshot.get("snapshot_id"):
        return str(snapshot["snapshot_id"])
    discovered = _deep_value(value, "repository_snapshot_id")
    if discovered:
        return str(discovered)
    discovered = _deep_value(value, "snapshot_id")
    return str(discovered) if discovered else None


def _graph_digest(value: Any) -> str | None:
    for key in ("graph_digest", "response_digest", "digest"):
        discovered = _deep_value(value, key)
        if isinstance(discovered, str) and discovered.strip():
            return discovered
    return None


def _first_match(value: Any) -> Mapping[str, Any]:
    for key in ("matches", "results", "nodes", "rows"):
        items = _deep_value(value, key)
        if isinstance(items, Sequence) and not isinstance(
            items, (str, bytes, bytearray)
        ):
            for item in items:
                if isinstance(item, Mapping):
                    return item
    return {}


def _match_path(value: Any) -> str | None:
    match = _first_match(value)
    path = match.get("path") or match.get("file") or match.get("relative_path")
    if isinstance(path, str) and path.strip():
        return path.replace("\\", "/")
    return None


def _match_symbol(value: Any) -> str | None:
    match = _first_match(value)
    symbol = match.get("name") or match.get("symbol") or match.get("qualified_name")
    if isinstance(symbol, str) and symbol.strip():
        return symbol
    return None


def _resume_args(value: Any, *, project: str, root: str) -> dict[str, Any] | None:
    raw = _deep_value(value, "index_next")
    if not isinstance(raw, Mapping):
        return None
    allowed = {
        "apply",
        "build_graph",
        "defer_graph",
        "force_refresh",
        "max_files_per_run",
        "expected_job_id",
        "expected_state_digest",
        "graph_only",
        "snapshot_id",
    }
    args = {key: raw[key] for key in allowed if key in raw}
    args.update(
        {
            "project": project,
            "root": root,
            "apply": True,
            "defer_graph": True,
            "max_files_per_run": MAX_FILES_PER_RUN,
        }
    )
    return args


def _graph_alignment(
    value: Any, expected_snapshot_id: str
) -> tuple[bool, str | None, str | None]:
    index = _deep_value(value, "index")
    graph = _deep_value(value, "graph")
    current_snapshot_id: str | None = None
    graph_snapshot_id: str | None = None
    if isinstance(index, Mapping):
        current = index.get("current_snapshot")
        if isinstance(current, Mapping) and current.get("snapshot_id"):
            current_snapshot_id = str(current["snapshot_id"])
    if isinstance(graph, Mapping) and graph.get("repository_snapshot_id"):
        graph_snapshot_id = str(graph["repository_snapshot_id"])
    aligned = bool(
        expected_snapshot_id
        and current_snapshot_id == expected_snapshot_id
        and graph_snapshot_id == expected_snapshot_id
    )
    return aligned, current_snapshot_id, graph_snapshot_id


async def _poll_graph_alignment(
    runner: Any,
    *,
    project: str,
    root: str,
    snapshot_id: str,
    timeout_seconds: float,
    interval_seconds: float = 2.0,
) -> Any:
    """Poll status after one deferred graph request; never enqueue duplicates."""

    deadline = time.monotonic() + timeout_seconds
    attempt = 0
    last_status: Any = None
    while time.monotonic() < deadline:
        attempt += 1
        remaining = max(1.0, deadline - time.monotonic())
        last_status = await runner.call(
            None,
            "bhm_index_status",
            {"project": project, "root": root},
            timeout=min(30.0, remaining),
            counts_toward_catalog=False,
            stage=f"graph-poll-{attempt:03d}",
            note=f"poll deferred graph alignment for {snapshot_id}",
        )
        aligned, current_snapshot_id, graph_snapshot_id = _graph_alignment(
            last_status, snapshot_id
        )
        if aligned:
            return last_status
        runner.state["graph_poll_last"] = {
            "attempt": attempt,
            "expected_snapshot_id": snapshot_id,
            "current_snapshot_id": current_snapshot_id,
            "graph_repository_snapshot_id": graph_snapshot_id,
        }
        await asyncio.sleep(
            min(interval_seconds, max(0.0, deadline - time.monotonic()))
        )
    raise TimeoutError(
        f"deferred graph did not align to repository snapshot {snapshot_id}"
    )


async def run_code_tools(
    runner: Any, state: MutableMapping[str, Any]
) -> dict[str, Any]:
    """Call and retain receipts for every public code MCP tool (163-186)."""
    project = str(state.get("repo_project") or PROJECT)
    root = str(state.get("repo_root_argument") or ROOT)
    results: dict[str, Any] = {}
    harness_errors: list[dict[str, str]] = []

    async def call(
        number: int,
        name: str,
        args: dict[str, Any],
        *,
        timeout: float = 120.0,
        expected_empty: bool = False,
        note: str,
        result_key: str | None = None,
    ) -> Any:
        key = result_key or name
        try:
            result = await runner.call(
                number,
                name,
                args,
                timeout=timeout,
                expected_empty=expected_empty,
                note=note,
                stage=key,
            )
        except (
            Exception
        ) as exc:  # Preserve full-catalog execution after one failed tool.
            result = {
                "harness_error": type(exc).__name__,
                "detail": str(exc)[:500],
            }
            harness_errors.append(
                {
                    "tool": name,
                    "error": type(exc).__name__,
                    "detail": str(exc)[:500],
                }
            )
        results[key] = result
        return result

    # 163: prove the read-only plan, apply bounded index slices, then construct
    # the graph separately from the completed authoritative snapshot.
    await call(
        163,
        "bhm_index_repository",
        {
            "project": project,
            "root": root,
            "apply": False,
            "build_graph": True,
            "force_refresh": False,
            "max_files_per_run": MAX_FILES_PER_RUN,
            "defer_graph": True,
        },
        timeout=180.0,
        note="read-only bounded indexing plan for Jmaka",
        result_key="bhm_index_repository_plan",
    )
    index_result = await call(
        163,
        "bhm_index_repository",
        {
            "project": project,
            "root": root,
            "apply": True,
            "build_graph": True,
            "force_refresh": False,
            "max_files_per_run": MAX_FILES_PER_RUN,
            "defer_graph": True,
        },
        timeout=900.0,
        note="apply first bounded Jmaka index slice with graph deferred",
        result_key="bhm_index_repository_apply",
    )

    for slice_number in range(1, MAX_RESUME_SLICES + 1):
        if _index_status(index_result) == "completed":
            break
        next_args = _resume_args(index_result, project=project, root=root)
        if not next_args:
            break
        index_result = await call(
            163,
            "bhm_index_repository",
            next_args,
            timeout=900.0,
            note=f"resume bounded Jmaka index slice {slice_number}",
            result_key=f"bhm_index_repository_resume_{slice_number:02d}",
        )

    snapshot_id = _snapshot_id(index_result)
    if not snapshot_id:
        raise RuntimeError("completed repository index did not return a snapshot id")
    await call(
        163,
        "bhm_index_repository",
        {
            "project": project,
            "root": root,
            "apply": True,
            "build_graph": True,
            "defer_graph": True,
            "graph_only": True,
            "snapshot_id": snapshot_id,
            "max_files_per_run": MAX_FILES_PER_RUN,
        },
        timeout=180.0,
        note="enqueue one deferred graph build from the completed index snapshot",
        result_key="bhm_index_repository_graph_only",
    )
    graph_status = await _poll_graph_alignment(
        runner,
        project=project,
        root=root,
        snapshot_id=snapshot_id,
        timeout_seconds=runner.index_timeout_seconds,
    )
    graph_digest = _graph_digest(graph_status)

    await call(
        164,
        "bhm_index_status",
        {"project": project, "root": root},
        note="read authoritative Jmaka index and graph freshness",
    )
    await call(
        165,
        "bhm_list_projects",
        {},
        note="confirm Jmaka is published in the SQLite repository index",
    )
    await call(
        166,
        "bhm_watch_repository",
        {
            "project": project,
            "root": root,
            "apply": False,
            "cycles": 1,
            "interval_seconds": 0.0,
            "build_graph": True,
            "defer_graph": True,
        },
        timeout=180.0,
        note="read-only bounded watcher plan; no daemon and no Jmaka write",
    )

    graph_search = await call(
        167,
        "bhm_search_graph",
        {"query": "main", "project": project, "root": root, "limit": 32},
        note="search Jmaka graph for an entry-point-like symbol",
    )
    symbol = _match_symbol(graph_search) or "main"
    code_search = await call(
        168,
        "bhm_search_code",
        {
            "query": symbol,
            "project": project,
            "root": root,
            "mode": "text",
            "limit": 32,
            "offset": 0,
            "include_snippets": True,
            "semantic_fusion": True,
            "semantic_weight": 0.35,
            "semantic_query": [symbol, "application entry point"],
            "semantic_min_score": 0.0,
            "max_tokens": 4096,
            "time_budget_ms": 1000.0,
        },
        timeout=180.0,
        note="exercise lexical search, redacted snippets and semantic fusion on Jmaka",
    )
    path = _match_path(code_search) or _match_path(graph_search) or "README.md"
    await call(
        169,
        "bhm_get_code_snippet",
        {"path": path, "line": 1, "context": 3, "project": project, "root": root},
        note="read a bounded redacted Jmaka snippet from an indexed path",
    )

    artifact_export = await call(
        170,
        "bhm_export_graph_artifact",
        {"project": project, "root": root, "apply": True},
        timeout=300.0,
        note="export Jmaka graph to a local non-authoritative runtime artifact",
    )
    artifact_path = _deep_value(artifact_export, "path")
    artifact_path = str(artifact_path) if artifact_path else ""
    await call(
        171,
        "bhm_verify_graph_artifact",
        {"path": artifact_path, "project": project, "root": root},
        timeout=180.0,
        note="verify Jmaka graph artifact checksum, provenance and schema",
    )
    await call(
        172,
        "bhm_plan_graph_artifact_promotion",
        {
            "path": artifact_path,
            "project": project,
            "root": root,
            "detached_signature_b64": None,
            "detached_public_key_b64": None,
            "adoption_receipt_digest": None,
            "rollback_anchor_snapshot_id": snapshot_id,
            "rollback_anchor_digest": graph_digest,
        },
        timeout=180.0,
        note="build proposal-only human-gated artifact promotion plan",
    )
    await call(
        173,
        "bhm_query_graph",
        {
            "query": symbol,
            "operation": "symbol",
            "project": project,
            "root": root,
            "depth": 2,
            "limit": 32,
            "offset": 0,
        },
        note="run allowlisted metadata-only symbol graph query",
    )
    await call(
        174,
        "bhm_query_graph_dsl",
        {
            "query": "MATCH (a:File)-[:contains]->(b:Function) RETURN a.path, b.name LIMIT 16",
            "project": project,
            "root": root,
            "limit": 16,
            "offset": 0,
            "time_budget_ms": 1000.0,
        },
        note="run bounded read-only graph DSL query over Jmaka metadata",
    )
    await call(
        175,
        "bhm_get_graph_schema",
        {"project": project, "root": root},
        note="read graph schema, parser digest and allowlisted operations",
    )
    await call(
        176,
        "bhm_check_index_coverage",
        {"project": project, "root": root},
        note="evaluate Jmaka freshness and fail-closed parser coverage",
    )
    await call(
        177,
        "bhm_get_architecture",
        {"project": project, "root": root},
        note="derive bounded Jmaka architecture summary from the graph",
    )
    await call(
        178,
        "bhm_resolve_packages",
        {"project": project, "root": root, "limit": 64},
        note="resolve package and module identities from Jmaka manifests",
    )
    await call(
        179,
        "bhm_dependency_provenance",
        {"project": project, "root": root, "limit": 64},
        note="inventory Jmaka lockfile provenance as metadata only",
    )
    await call(
        180,
        "bhm_type_references",
        {"project": project, "root": root, "limit": 128},
        note="derive Jmaka inheritance, aliases and import references",
    )
    await call(
        181,
        "bhm_bicep_module_resolution",
        {"project": project, "root": root, "limit": 64},
        expected_empty=True,
        note="resolve literal Bicep modules; empty is valid for non-Bicep Jmaka",
    )
    await call(
        182,
        "bhm_trace_path",
        {
            "query": symbol,
            "project": project,
            "root": root,
            "operation": "callers",
            "depth": 3,
            "limit": 64,
        },
        note="trace bounded caller paths for a discovered Jmaka symbol",
    )
    await call(
        183,
        "bhm_trace_graph",
        {"project": project, "limit": 128},
        note="build bounded evidence-only Jmaka cross-service trace graph",
    )
    await call(
        184,
        "bhm_cross_repo_links",
        {"project": project, "limit": 128},
        note="propose metadata-only cross-repository links for Jmaka",
    )
    await call(
        185,
        "bhm_change_impact",
        {
            "query": symbol,
            "project": project,
            "root": root,
            "depth": 3,
            "limit": 64,
        },
        note="analyze bounded graph impact for a discovered Jmaka symbol",
    )
    await call(
        186,
        "bhm_change_impact_preview",
        {
            "project": project,
            "root": root,
            "changed_paths": [path],
            "base_revision": None,
            "expected_graph_digest": graph_digest,
            "include_git_history": False,
        },
        note="preview impact of a synthetic Jmaka path change without worktree writes",
    )

    phase_state = {
        "project": project,
        "root": root,
        "snapshot_id": snapshot_id,
        "graph_digest": graph_digest,
        "artifact_path": artifact_path,
        "probe_symbol": symbol,
        "probe_path": path,
        "index_status": _index_status(index_result),
        "harness_errors": harness_errors,
        "results": results,
    }
    state["code_tools"] = phase_state
    state["snapshot_id"] = snapshot_id
    state["graph_digest"] = graph_digest
    state["artifact_path"] = artifact_path
    return phase_state


__all__ = ["run_code_tools"]
