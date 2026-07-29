"""Deterministic local benchmark for WI-170 GN/KDL metadata parsing."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import PurePosixPath

from blackholememory.code_graph import _GraphDraft
from blackholememory.code_graph import _extract_gn_metadata
from blackholememory.code_graph import _extract_kdl_metadata
from blackholememory.code_graph import _file_node_key
from blackholememory.code_graph import _node
from blackholememory.code_graph import _sha256


FIXTURES = {
    "build.gn": (
        'import("toolchain.gni")\n'
        'executable("app") { sources = [ "src/main.cc" ]; deps = [ ":core" ]; script = "private-script.py" }\n'
        'static_library("core") { sources = [ "src/core.cc" ] }\n'
    ),
    "config.kdl": (
        'server "prod" token="private-token" {\n'
        '  tls enabled=true certificate="private-cert";\n'
        '  listener port=8443 { route "/internal" backend="secret-backend"; }\n'
        '}\n'
    ),
}
ITERATIONS = 1_000


def main() -> int:
    fixture_bytes = "\n".join(f"{path}\n{content}" for path, content in sorted(FIXTURES.items())).encode()
    fixture_digest = hashlib.sha256(fixture_bytes).hexdigest()
    durations: list[float] = []
    first: dict[str, object] | None = None
    for _ in range(ITERATIONS):
        draft = _GraphDraft("bench-root", {"project": "wi170", "root_path": "."})
        observed: dict[str, object] = {}
        started = time.perf_counter_ns()
        for path, content in sorted(FIXTURES.items()):
            language = "gn" if path.endswith(".gn") else "kdl"
            file_hash = _sha256(content)
            file_key = _file_node_key(draft.root_id, path)
            draft.file_paths.add(path)
            draft.add_node(
                _node(
                    root_id=draft.root_id,
                    stable_key=file_key,
                    kind="file",
                    path=path,
                    name=PurePosixPath(path).name,
                    qualified_name=path,
                    language=language,
                    content_sha256=file_hash,
                    attributes={"metadata_only": True},
                )
            )
            extractor = _extract_gn_metadata if language == "gn" else _extract_kdl_metadata
            status, error = extractor(draft, path, file_key, content, file_hash)
            observed[path] = {"status": status, "error": error}
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        serialized = json.dumps({"nodes": draft.nodes, "edges": draft.edges}, sort_keys=True)
        result = {
            "files": observed,
            "node_count": len(draft.nodes),
            "edge_count": len(draft.edges),
            "metadata_only": all(bool(node.get("attributes", {}).get("metadata_only")) for node in draft.nodes.values()),
            "raw_values": any(value in serialized for value in ("private-script.py", "private-token", "private-cert", "secret-backend")),
        }
        if first is None:
            first = result
        elif result != first:
            raise SystemExit("non-deterministic GN/KDL parser result")
    ordered = sorted(durations)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    output = {
        "schema_version": "bhm.p28.wi170.gn-kdl-benchmark.v1",
        "extractor_version": "bhm.code-graph.extractor.v34",
        "parser_ids": ["gn-build-regex", "kdl-document-regex"],
        "fixture_digest": fixture_digest,
        "result": first,
        "iterations": ITERATIONS,
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(durations), 3),
        "deterministic": True,
        "execution": {
            "metadata_only": True,
            "gn_evaluation": False,
            "kdl_value_decoding": False,
            "compile_or_execute": False,
            "secrets_read": False,
            "network": False,
            "store_writes": False,
            "edge_promotion": False,
        },
    }
    output["ok"] = bool(first and all(item["status"] == "parsed" and not item["error"] for item in first["files"].values()) and not first["raw_values"])
    print(json.dumps(output, indent=2))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
