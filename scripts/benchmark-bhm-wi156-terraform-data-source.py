#!/usr/bin/env python
"""Bounded benchmark for WI-156 Terraform data-source metadata."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path
from statistics import quantiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from blackholememory.code_graph import CODE_GRAPH_EXTRACTOR_VERSION  # noqa: E402
from blackholememory.code_graph import _GraphDraft  # noqa: E402
from blackholememory.code_graph import _extract_terraform_iac_edges  # noqa: E402
from blackholememory.code_graph import _file_node_key  # noqa: E402
from blackholememory.code_graph import _node  # noqa: E402
from blackholememory.code_graph import _sha256  # noqa: E402


FIXTURE = (
    'data "aws_ami" "ubuntu" {\n'
    '  provider = aws.west\n'
    '  depends_on = [aws_subnet.private]\n'
    '}\n'
    'resource "aws_subnet" "private" {}\n'
    'provider "aws" { alias = "west" }\n'
)


def _once() -> dict[str, object]:
    draft = _GraphDraft("root-wi156", {})
    path = "main.tf"
    file_key = _file_node_key(draft.root_id, path)
    digest = _sha256(FIXTURE)
    draft.add_node(_node(root_id=draft.root_id, stable_key=file_key, kind="file", path=path, name=path, qualified_name=path, language="config", start_line=1, end_line=len(FIXTURE.splitlines()), content_sha256=digest, parser_version=CODE_GRAPH_EXTRACTOR_VERSION))
    _extract_terraform_iac_edges(draft, path, file_key, FIXTURE)
    data_nodes = [node for node in draft.nodes.values() if node.get("node_kind") == "infrastructure_data_source"]
    evidence = sorted(str(edge.get("attributes", {}).get("evidence_class") or "") for edge in draft.edges.values())
    return {
        "data_nodes": sorted(str(node.get("name") or "") for node in data_nodes),
        "edge_evidence": evidence,
        "metadata_only": all(bool(node.get("attributes", {}).get("metadata_only")) for node in data_nodes),
        "raw_source": any("content" in node or "raw_source" in node for node in draft.nodes.values()),
    }


def run(iterations: int = 1000) -> dict[str, object]:
    count = max(1, min(int(iterations), 5000))
    first = _once()
    samples: list[float] = []
    deterministic = True
    for _ in range(count):
        start = time.perf_counter()
        observed = _once()
        samples.append((time.perf_counter() - start) * 1000.0)
        deterministic = deterministic and observed == first
    ordered = sorted(samples)
    p95 = quantiles(ordered, n=20, method="inclusive")[18] if len(ordered) > 1 else ordered[0]
    digest = hashlib.sha256(json.dumps(first, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    result = {
        "schema_version": "bhm.p28.wi156.terraform-data-source-benchmark.v1",
        "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
        "iterations": count,
        "result": first,
        "p50_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(ordered), 3),
        "deterministic": deterministic,
        "fixture_digest": digest,
        "execution": {"raw_source_returned": False, "network": False, "compiler_or_lsp": False, "store_writes": False, "terraform_apply": False},
    }
    result["ok"] = bool(deterministic and float(result["p95_ms"]) <= 20 and all(not value for value in result["execution"].values()))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    result = run(args.iterations)
    print(json.dumps(result, indent=2) + "\n", end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
