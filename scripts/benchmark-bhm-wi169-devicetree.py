"""Deterministic local benchmark for WI-169 DeviceTree metadata parsing."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import PurePosixPath

from blackholememory.code_graph import _GraphDraft, _extract_devicetree_metadata, _file_node_key, _node, _sha256


FIXTURES = {
    "board.dts": "/dts-v1/;\n#include \"soc.dtsi\"\n/ { uart0: serial@1000 { compatible = \"vendor,uart\"; status = \"okay\"; }; };\n",
    "fix.overlay": "&uart0 { status = \"disabled\"; };\n",
}
ITERATIONS = 1_000


def main() -> int:
    fixture_bytes = "\n".join(f"{path}\n{content}" for path, content in sorted(FIXTURES.items())).encode()
    fixture_digest = hashlib.sha256(fixture_bytes).hexdigest()
    durations: list[float] = []
    first: dict[str, object] | None = None
    for _ in range(ITERATIONS):
        draft = _GraphDraft("bench-root", {"project": "wi169", "root_path": "."})
        observed: dict[str, object] = {}
        started = time.perf_counter_ns()
        for path, content in sorted(FIXTURES.items()):
            file_hash = _sha256(content)
            file_key = _file_node_key(draft.root_id, path)
            draft.file_paths.add(path)
            draft.add_node(_node(root_id=draft.root_id, stable_key=file_key, kind="file", path=path, name=PurePosixPath(path).name, qualified_name=path, language="devicetree", content_sha256=file_hash, attributes={"metadata_only": True}))
            status, error = _extract_devicetree_metadata(draft, path, file_key, content, file_hash)
            observed[path] = {"status": status, "error": error}
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        serialized = json.dumps(draft.nodes, sort_keys=True)
        result = {
            "files": observed,
            "node_count": len(draft.nodes),
            "edge_count": len(draft.edges),
            "metadata_only": all(bool(node.get("attributes", {}).get("metadata_only")) for node in draft.nodes.values()),
            "raw_values": any(value in serialized for value in ("vendor,uart", "disabled", "soc.dtsi")),
        }
        if first is None:
            first = result
        elif result != first:
            raise SystemExit("non-deterministic DeviceTree parser result")
    ordered = sorted(durations)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    output = {
        "schema_version": "bhm.p28.wi169.devicetree-benchmark.v1",
        "extractor_version": "bhm.code-graph.extractor.v34",
        "parser_id": "devicetree-metadata-regex",
        "fixture_digest": fixture_digest,
        "result": first,
        "iterations": ITERATIONS,
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(durations), 3),
        "deterministic": True,
        "execution": {"metadata_only": True, "phandle_evaluation": False, "preprocessing": False, "compile_or_execute": False, "secrets_read": False, "network": False, "store_writes": False, "edge_promotion": False},
    }
    output["ok"] = bool(first and all(item["status"] == "parsed" and not item["error"] for item in first["files"].values()) and not first["raw_values"])
    print(json.dumps(output, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
