#!/usr/bin/env python
"""Bounded offline benchmark for the WI-162 GitHub Actions parser."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from statistics import quantiles

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.code_graph import CODE_GRAPH_EXTRACTOR_VERSION  # noqa: E402
from blackholememory.code_graph import PARSER_REGISTRY  # noqa: E402
from blackholememory.code_graph import _GraphDraft  # noqa: E402
from blackholememory.code_graph import _extract_github_actions_workflow  # noqa: E402
from blackholememory.code_graph import _file_node_key  # noqa: E402
from blackholememory.code_graph import _node  # noqa: E402
from blackholememory.code_graph import _sha256  # noqa: E402


FIXTURE_PATH = ".github/workflows/ci.yml"
FIXTURE = (
    "name: CI secret-name\n"
    "on:\n"
    "  push:\n"
    "jobs:\n"
    "  test:\n"
    "    steps:\n"
    "      - name: Checkout source\n"
    "        uses: actions/checkout@v4\n"
    "      - name: Run tests\n"
    "        run: npm test -- --token SECRET\n"
    "  build:\n"
    "    needs: [test]\n"
    "    steps:\n"
    "      - run: echo build\n"
)


def _run_once() -> dict[str, object]:
    draft = _GraphDraft("root-wi162", {})
    file_key = _file_node_key(draft.root_id, FIXTURE_PATH)
    digest = _sha256(FIXTURE)
    draft.add_node(
        _node(
            root_id=draft.root_id,
            stable_key=file_key,
            kind="file",
            path=FIXTURE_PATH,
            name="ci.yml",
            qualified_name=FIXTURE_PATH,
            language="github-actions",
            start_line=1,
            end_line=len(FIXTURE.splitlines()),
            content_sha256=digest,
            parser_version=PARSER_REGISTRY["github-actions"]["version"],
        )
    )
    status, error = _extract_github_actions_workflow(draft, FIXTURE_PATH, file_key, FIXTURE, digest)
    nodes = list(draft.nodes.values())
    edges = list(draft.edges.values())
    return {
        "status": status,
        "error": error,
        "node_kinds": sorted(str(node.get("node_kind") or "") for node in nodes),
        "edge_kinds": sorted(str(edge.get("edge_kind") or "") for edge in edges),
        "node_count": len(nodes),
        "edge_count": len(edges),
        "metadata_only": all(bool(node.get("attributes", {}).get("metadata_only")) for node in nodes if node.get("node_kind") != "file"),
        "raw_values": any(value in json.dumps(nodes + edges, sort_keys=True) for value in ("SECRET", "npm test", "actions/checkout@v4")),
    }


def run(iterations: int = 1000) -> dict[str, object]:
    count = max(1, min(int(iterations), 5000))
    first = _run_once()
    latencies: list[float] = []
    deterministic = True
    for _index in range(count):
        started = time.perf_counter()
        observed = _run_once()
        latencies.append((time.perf_counter() - started) * 1000.0)
        deterministic = deterministic and observed == first
    ordered = sorted(latencies)
    p95 = quantiles(ordered, n=20, method="inclusive")[18] if len(ordered) > 1 else ordered[0]
    fixture_digest = hashlib.sha256(FIXTURE.encode("utf-8")).hexdigest()
    result: dict[str, object] = {
        "schema_version": "bhm.p28.wi162.github-actions-benchmark.v1",
        "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
        "parser_id": PARSER_REGISTRY["github-actions"]["parser_id"],
        "fixture_path": FIXTURE_PATH,
        "fixture_digest": fixture_digest,
        "result": first,
        "iterations": count,
        "p50_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(ordered), 3),
        "deterministic": deterministic,
        "execution": {
            "raw_source_returned": False,
            "raw_values_returned": bool(first["raw_values"]),
            "network": False,
            "command_execution": False,
            "secrets_read": False,
            "store_writes": False,
            "edge_promotion": False,
        },
    }
    result["ok"] = bool(deterministic and not first["raw_values"] and float(result["p95_ms"]) <= 20.0)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.iterations)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.write_text(payload, encoding="utf-8")
    print(payload, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
