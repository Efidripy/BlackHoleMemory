"""Deterministic local benchmark for WI-168 LLVM IR/TableGen metadata parsing."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import PurePosixPath

from blackholememory.code_graph import _GraphDraft, _extract_llvm_tablegen_structural, _file_node_key, _node, _sha256

FIXTURE_PATH = "fixture.ll"
FIXTURE = "source_filename = \"module.c\"\n%State = type { i32, ptr }\n@counter = global i32 0\ndeclare void @helper(ptr)\ndefine void @run(ptr %p) {\n  call void @helper(ptr %p)\n  %v = load i32, ptr @counter\n  ret void\n}\n"
TABLEGEN_PATH = "fixture.td"
TABLEGEN = "include \"Base.td\"\nclass Register<string n> { let Namespace = n; }\ndef CPU : Register<\"private-value\">;\n"
ITERATIONS = 1_000


def _run(path: str, content: str, language: str) -> dict[str, object]:
    file_hash = _sha256(content)
    draft = _GraphDraft("bench-root", {"project": "wi168", "root_path": "."})
    file_key = _file_node_key(draft.root_id, path)
    draft.file_paths.add(path)
    draft.add_node(_node(root_id=draft.root_id, stable_key=file_key, kind="file", path=path, name=PurePosixPath(path).name, qualified_name=path, language=language, content_sha256=file_hash, attributes={"metadata_only": True}))
    started = time.perf_counter_ns()
    status, error = _extract_llvm_tablegen_structural(draft, path, file_key, content, file_hash, language)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000
    payload = json.dumps(draft.nodes, sort_keys=True) + json.dumps(draft.edges, sort_keys=True)
    return {"status": status, "error": error, "node_kinds": sorted(str(node["node_kind"]) for node in draft.nodes.values()), "edge_kinds": sorted(str(edge["edge_kind"]) for edge in draft.edges.values()), "node_count": len(draft.nodes), "edge_count": len(draft.edges), "raw_values": any(value in payload for value in ("private-value", "i32, ptr", "module.c")), "elapsed_ms": elapsed}


def main() -> int:
    fixture_hash = hashlib.sha256((FIXTURE + TABLEGEN).encode()).hexdigest()
    durations: list[float] = []
    first: dict[str, object] | None = None
    for _ in range(ITERATIONS):
        llvm = _run(FIXTURE_PATH, FIXTURE, "llvm")
        tablegen = _run(TABLEGEN_PATH, TABLEGEN, "tablegen")
        result = {"llvm": {key: value for key, value in llvm.items() if key != "elapsed_ms"}, "tablegen": {key: value for key, value in tablegen.items() if key != "elapsed_ms"}}
        durations.append(float(llvm["elapsed_ms"]) + float(tablegen["elapsed_ms"]))
        if first is None:
            first = result
        elif result != first:
            raise SystemExit("non-deterministic LLVM/TableGen parser result")
    ordered = sorted(durations)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    output = {"schema_version": "bhm.p28.wi168.llvm-tablegen-benchmark.v1", "extractor_version": "bhm.code-graph.extractor.v34", "parser_ids": ["llvm-ir-regex", "tablegen-regex"], "fixture_paths": [FIXTURE_PATH, TABLEGEN_PATH], "fixture_digest": fixture_hash, "result": first, "iterations": ITERATIONS, "p50_ms": round(p50, 3), "p95_ms": round(p95, 3), "max_ms": round(max(durations), 3), "deterministic": True, "execution": {"metadata_only": True, "execution": False, "type_checking": False, "abi_or_lsp": False, "macro_evaluation": False, "include_expansion": False, "secrets_read": False, "network": False, "store_writes": False, "edge_promotion": False}}
    output["ok"] = bool(first and all(not bool(item.get("error")) and not bool(item.get("raw_values")) for item in first.values()))
    print(json.dumps(output, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
