#!/usr/bin/env python
"""Deterministic local benchmark for WI-173 Compose/Kustomize metadata."""

from __future__ import annotations

import hashlib
import json
import statistics
import time
from pathlib import PurePosixPath

from blackholememory.code_graph import _GraphDraft
from blackholememory.code_graph import _extract_service_edges
from blackholememory.code_graph import _file_node_key
from blackholememory.code_graph import _node
from blackholememory.code_graph import _sha256


FIXTURES = {
    "compose.yaml": (
        "services:\n  api:\n    volumes:\n      - appdata:/var/lib/app:ro\n"
        "    networks:\n      - frontend\n    environment:\n      API_TOKEN: ${API_TOKEN}\n"
        "volumes:\n  appdata:\nnetworks:\n  frontend:\n"
    ),
    "kustomization.yaml": (
        "apiVersion: kustomize.config.k8s.io/v1beta1\nkind: Kustomization\n"
        "configMapGenerator:\n  - name: app-config\n    envs:\n      - app.env\n"
        "secretGenerator:\n  - name: app-secret\n    files:\n      - token.txt\n"
        "volumes:\n  - cache-volume\nnetworks:\n  - frontend\n"
    ),
}
ITERATIONS = 1_000


def main() -> int:
    fixture_bytes = "\n".join(f"{path}\n{content}" for path, content in sorted(FIXTURES.items())).encode()
    fixture_digest = hashlib.sha256(fixture_bytes).hexdigest()
    durations: list[float] = []
    first: dict[str, object] | None = None
    for _ in range(ITERATIONS):
        draft = _GraphDraft("bench-root", {"project": "wi173", "root_path": "."})
        observed: dict[str, object] = {}
        started = time.perf_counter_ns()
        for path, content in sorted(FIXTURES.items()):
            file_hash = _sha256(content)
            file_key = _file_node_key(draft.root_id, path)
            draft.file_paths.add(path)
            draft.add_node(_node(root_id=draft.root_id, stable_key=file_key, kind="file", path=path, name=PurePosixPath(path).name, qualified_name=path, language="yaml", content_sha256=file_hash, attributes={"metadata_only": True}))
            _extract_service_edges(draft, path, file_key, content)
            observed[path] = {"status": "parsed", "error": ""}
        durations.append((time.perf_counter_ns() - started) / 1_000_000)
        serialized = json.dumps({"nodes": draft.nodes, "edges": draft.edges}, sort_keys=True)
        wi173_nodes = [node for node in draft.nodes.values() if str(node.get("attributes", {}).get("evidence_class", "")).startswith(("compose-", "kustomize-"))]
        result = {
            "files": observed,
            "node_count": len(draft.nodes),
            "edge_count": len(draft.edges),
            "wi173_node_count": len(wi173_nodes),
            "metadata_only": all(bool(node.get("attributes", {}).get("metadata_only")) for node in wi173_nodes),
            "raw_values": any(value in serialized for value in ("API_TOKEN", "${API_TOKEN}", "/var/lib/app", "app-config", "app.env", "token.txt")),
        }
        if first is None:
            first = result
        elif result != first:
            raise SystemExit("non-deterministic Compose/Kustomize metadata result")
    ordered = sorted(durations)
    p50 = statistics.median(ordered)
    p95 = ordered[int(len(ordered) * 0.95) - 1]
    output = {
        "schema_version": "bhm.p28.wi173.compose-kustomize-benchmark.v1",
        "extractor_version": "bhm.code-graph.extractor.v35",
        "fixture_digest": fixture_digest,
        "result": first,
        "iterations": ITERATIONS,
        "p50_ms": round(p50, 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(durations), 3),
        "deterministic": True,
        "execution": {"metadata_only": True, "yaml_evaluation": False, "compile_or_execute": False, "secrets_read": False, "network": False, "store_writes": False, "edge_promotion": False, "raw_values": False},
    }
    output["ok"] = bool(first and first["wi173_node_count"] > 0 and first["metadata_only"] and not first["raw_values"])
    print(json.dumps(output, indent=2, sort_keys=True))
    return 0 if output["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
