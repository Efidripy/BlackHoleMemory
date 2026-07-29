"""Deterministic local benchmark for the WI-164 Starlark/Bazel overlay."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import PurePosixPath

from blackholememory.code_graph import _GraphDraft
from blackholememory.code_graph import _extract_starlark_metadata
from blackholememory.code_graph import _file_node_key
from blackholememory.code_graph import _node
from blackholememory.code_graph import _sha256


FIXTURE_PATH = "BUILD.bazel"
FIXTURE = """load(\"//tools:rules.bzl\", \"custom_rule\")
cc_library(
  name = \"app\",
  deps = [\":core\", \"//lib:base\"],
  srcs = [\"main.cc\"],
)
"""
ITERATIONS = 1_000
P95_BUDGET_MS = 20.0


def main() -> int:
    fixture_bytes = FIXTURE.encode("utf-8")
    fixture_hash = hashlib.sha256(fixture_bytes).hexdigest()
    file_hash = _sha256(fixture_bytes)
    durations: list[float] = []
    first: dict[str, object] | None = None
    for _ in range(ITERATIONS):
        draft = _GraphDraft("bench-root", {"project": "wi164", "root_path": "."})
        file_key = _file_node_key(draft.root_id, FIXTURE_PATH)
        draft.file_paths.add(FIXTURE_PATH)
        draft.add_node(_node(root_id=draft.root_id, stable_key=file_key, kind="file", path=FIXTURE_PATH, name=PurePosixPath(FIXTURE_PATH).name, qualified_name=FIXTURE_PATH, language="starlark", content_sha256=file_hash, attributes={"metadata_only": True}))
        started = time.perf_counter_ns()
        status, error = _extract_starlark_metadata(draft, FIXTURE_PATH, file_key, FIXTURE, file_hash)
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        serialized = json.dumps(draft.nodes, sort_keys=True)
        result = {
            "status": status,
            "error": error,
            "node_kinds": sorted(str(node["node_kind"]) for node in draft.nodes.values()),
            "edge_kinds": sorted(str(edge["edge_kind"]) for edge in draft.edges.values()),
            "node_count": len(draft.nodes),
            "edge_count": len(draft.edges),
            "metadata_only": all(bool(node.get("attributes", {}).get("metadata_only")) for node in draft.nodes.values() if node.get("node_kind") != "file"),
            "raw_values": any(value in serialized for value in ("//tools:rules.bzl", "//lib:base", "main.cc", "custom_rule")),
        }
        if first is None:
            first = result
        elif result != first:
            raise SystemExit("non-deterministic Starlark parser result")
    ordered = sorted(durations)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    output = {
        "schema_version": "bhm.p28.wi164.starlark-bazel-benchmark.v1",
        "extractor_version": "bhm.code-graph.extractor.v34",
        "parser_id": "starlark-bazel-regex",
        "fixture_path": FIXTURE_PATH,
        "fixture_digest": fixture_hash,
        "result": first,
        "iterations": ITERATIONS,
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(durations), 3),
        "deterministic": True,
        "execution": {
            "raw_source_returned": False,
            "raw_values_returned": bool(first and first.get("raw_values")),
            "network": False,
            "macro_or_load_execution": False,
            "rule_execution": False,
            "secrets_read": False,
            "store_writes": False,
            "edge_promotion": False,
        },
    }
    output["ok"] = bool(first and first["status"] == "parsed" and not first["error"] and not first["raw_values"] and p95 <= P95_BUDGET_MS)
    print(json.dumps(output, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
