#!/usr/bin/env python
"""Validate the bounded WI-180 live semantic evidence slice.

The probe consumes the existing read-only WI-82 runtime audit and adds the
missing live assertions: the explicit operator flag must be enabled, at least
one semantic query must be active, all observed rows must remain projection-
only, and the authoritative runtime/freshness gate must be healthy.  It never
enables the flag, starts a model, returns source/vectors, or writes state.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any, Mapping


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

_WI82_PATH = Path(__file__).with_name("validate-bhm-p28-wi82-semantic-quality.py")
_SPEC = importlib.util.spec_from_file_location("bhm_wi82_semantic_quality", _WI82_PATH)
if _SPEC is None or _SPEC.loader is None:  # pragma: no cover - packaging failure
    raise RuntimeError("unable to load WI-82 semantic quality probe")
_WI82 = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_WI82)

try:
    from blackholememory.caller_auth import configured_caller_token
except ImportError:  # pragma: no cover - local development fallback
    def configured_caller_token() -> str:
        return ""


SCHEMA_VERSION = "bhm.p28.wi180.live-semantic.v1"


def evaluate_live_gate(result: Mapping[str, Any]) -> dict[str, Any]:
    """Return deterministic bounded assertions without mutating the payload."""

    live = result.get("live") if isinstance(result.get("live"), Mapping) else {}
    runtime = live.get("runtime") if isinstance(live.get("runtime"), Mapping) else {}
    semantic = live.get("semantic") if isinstance(live.get("semantic"), Mapping) else {}
    rows = semantic.get("queries") if isinstance(semantic.get("queries"), list) else []
    failures: list[str] = []
    if not bool(live.get("ok")):
        failures.append("wi82_live_runtime_or_freshness_gate_failed")
    if str(semantic.get("state") or "").casefold() != "active":
        failures.append("semantic_state_not_active")
    if int(semantic.get("active_queries") or 0) < 1:
        failures.append("no_active_semantic_query")
    if not bool(runtime.get("ok")):
        failures.append("authoritative_runtime_provider_or_slo_not_ready")
    non_projection = [
        str(row.get("query") or "")
        for row in rows
        if not bool(row.get("active")) or not bool(row.get("projection_only"))
    ]
    if non_projection:
        failures.append("semantic_rows_not_active_projection_only")
    execution = live.get("execution") if isinstance(live.get("execution"), Mapping) else {}
    for key in ("writes_sqlite_state", "writes_qdrant", "model_started", "autonomous_apply", "raw_source_returned"):
        if bool(execution.get(key)):
            failures.append(f"execution_boundary_{key}_violated")
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": not failures,
        "semantic_state": semantic.get("state"),
        "active_queries": int(semantic.get("active_queries") or 0),
        "query_count": len(rows),
        "projection_only_rows": len(rows) - len(non_projection),
        "runtime_ok": bool(runtime.get("ok")),
        "freshness_ok": bool(live.get("freshness", {}).get("ok")) if isinstance(live.get("freshness"), Mapping) else False,
        "failures": failures,
        "execution": {
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "model_started": False,
            "autonomous_apply": False,
            "raw_source_returned": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default=os.getenv("BHM_CALLER_TOKEN", ""))
    parser.add_argument("--project", default="sojmieblo")
    parser.add_argument("--root", default=r"E:\GitHub\repos\sojmieblo")
    parser.add_argument("--query", dest="queries", action="append", default=None)
    parser.add_argument("--cases", type=int, default=16)
    args = parser.parse_args()
    token = str(args.token or configured_caller_token() or "")
    result = _WI82.run_gate(
        base_url=args.base_url,
        token=token,
        project=args.project,
        root=args.root,
        queries=tuple(args.queries or ("workManager", "graph", "runtime")),
        cases=args.cases,
        max_snapshot_age_seconds=_WI82.DEFAULT_MAX_SNAPSHOT_AGE_SECONDS,
    )
    gate = evaluate_live_gate(result)
    output = {
        "schema_version": SCHEMA_VERSION,
        "wi82": result,
        "gate": gate,
        "read_only": True,
        "operator_flag": "BHM_CODE_SEMANTIC_FUSION",
        "provider_policy": "preexisting-provider-only",
        "authority": "sqlite-authoritative",
        "semantic_layer": "mem0-logical",
        "projection_layer": "qdrant-projection-only",
    }
    output["ok"] = bool(result.get("benchmark", {}).get("ok") and gate.get("ok"))
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
