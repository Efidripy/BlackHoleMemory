"""Deterministic local benchmark for WI-166 Kconfig metadata parsing."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import PurePosixPath

from blackholememory.code_graph import _GraphDraft, _extract_kconfig_metadata, _file_node_key, _node, _sha256


FIXTURE_PATH = "Kconfig.debug"
FIXTURE = '''config BHM_CORE
    bool "BlackHoleMemory core"
    default y
    depends on NET && !BROKEN
    select BHM_GRAPH
source "subsystems/Kconfig"
rsource "local/Kconfig.debug"
'''
ITERATIONS = 1_000


def main() -> int:
    fixture_hash = hashlib.sha256(FIXTURE.encode()).hexdigest()
    file_hash = _sha256(FIXTURE)
    durations: list[float] = []
    first: dict[str, object] | None = None
    for _ in range(ITERATIONS):
        draft = _GraphDraft("bench-root", {"project": "wi166", "root_path": "."})
        file_key = _file_node_key(draft.root_id, FIXTURE_PATH)
        draft.file_paths.add(FIXTURE_PATH)
        draft.add_node(_node(root_id=draft.root_id, stable_key=file_key, kind="file", path=FIXTURE_PATH, name=PurePosixPath(FIXTURE_PATH).name, qualified_name=FIXTURE_PATH, language="kconfig", content_sha256=file_hash, attributes={"metadata_only": True}))
        started = time.perf_counter_ns()
        status, error = _extract_kconfig_metadata(draft, FIXTURE_PATH, file_key, FIXTURE, file_hash)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        payload = json.dumps(draft.nodes, sort_keys=True)
        result = {"status": status, "error": error, "node_kinds": sorted(str(node["node_kind"]) for node in draft.nodes.values()), "edge_kinds": sorted(str(edge["edge_kind"]) for edge in draft.edges.values()), "node_count": len(draft.nodes), "edge_count": len(draft.edges), "raw_values": any(value in payload for value in ("BlackHoleMemory core", "subsystems/Kconfig", "NET && !BROKEN"))}
        if first is None:
            first = result
        elif result != first:
            raise SystemExit("non-deterministic Kconfig parser result")
    ordered = sorted(durations)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    output = {"schema_version": "bhm.p28.wi166.kconfig-benchmark.v1", "extractor_version": "bhm.code-graph.extractor.v34", "parser_id": "kconfig-directive-regex", "fixture_path": FIXTURE_PATH, "fixture_digest": fixture_hash, "result": first, "iterations": ITERATIONS, "p50_ms": round(p50, 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "deterministic": True, "execution": {"metadata_only": True, "expression_evaluation": False, "include_expansion": False, "build_execution": False, "secrets_read": False, "network": False, "store_writes": False, "edge_promotion": False}}
    output["ok"] = bool(first and first["status"] == "parsed" and not first["error"] and not first["raw_values"])
    print(json.dumps(output, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
