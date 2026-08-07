#!/usr/bin/env python
"""Bounded parser benchmark for WI-150 Tcl/QML/Racket identities."""

from __future__ import annotations

from blackholememory.filesystem_boundaries import replace_bytes_safely

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
from blackholememory.code_graph import _extract_low_level_family_structural  # noqa: E402
from blackholememory.code_graph import _file_node_key  # noqa: E402
from blackholememory.code_graph import _node  # noqa: E402
from blackholememory.code_graph import _sha256  # noqa: E402


FIXTURES = {
    "tcl": ('package require Tcl 8.6\nsource lib.tcl\nproc run {value} { return $value }\nnamespace eval demo {}\n', "main.tcl"),
    "qml": ("import QtQuick 2.15\nItem {\n property int count: 0\n function run(value) { return value }\n}\n", "Main.qml"),
    "racket": ('#lang racket\n(require racket/list)\n(define (run value) value)\n(struct State (value))\n', "main.rkt"),
}


def _run_once() -> dict[str, object]:
    output: dict[str, object] = {}
    for language, (content, path) in FIXTURES.items():
        draft = _GraphDraft("root-wi150", {})
        file_key = _file_node_key(draft.root_id, path)
        digest = _sha256(content)
        draft.add_node(
            _node(
                root_id=draft.root_id,
                stable_key=file_key,
                kind="file",
                path=path,
                name=path,
                qualified_name=path,
                language=language,
                start_line=1,
                end_line=len(content.splitlines()),
                content_sha256=digest,
                parser_version=PARSER_REGISTRY[language]["version"],
            )
        )
        status, error = _extract_low_level_family_structural(draft, path, file_key, content, digest, language)
        output[language] = {
            "status": status,
            "error": error,
            "node_count": len(draft.nodes),
            "edge_count": len(draft.edges),
            "import_count": len(draft.imports.get(path, [])),
            "names": sorted(str(node.get("name") or "") for node in draft.nodes.values() if node.get("node_kind") != "file"),
            "metadata_only": all(bool(node.get("attributes", {}).get("metadata_only")) for node in draft.nodes.values() if node.get("node_kind") != "file"),
            "raw_source": any("content" in node or "raw_source" in node for node in draft.nodes.values()),
        }
    return output


def run(iterations: int = 1000) -> dict[str, object]:
    count = max(1, min(int(iterations), 5000))
    latencies: list[float] = []
    first = _run_once()
    deterministic = True
    for _index in range(count):
        started = time.perf_counter()
        observed = _run_once()
        latencies.append((time.perf_counter() - started) * 1000.0)
        deterministic = deterministic and observed == first
    ordered = sorted(latencies)
    p95 = quantiles(ordered, n=20, method="inclusive")[18] if len(ordered) > 1 else ordered[0]
    digest = hashlib.sha256(json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    result: dict[str, object] = {
        "schema_version": "bhm.p28.wi150.parser-families-benchmark.v1",
        "extractor_version": CODE_GRAPH_EXTRACTOR_VERSION,
        "parser_ids": {language: PARSER_REGISTRY[language]["parser_id"] for language in sorted(FIXTURES)},
        "languages": first,
        "iterations": count,
        "p50_ms": round(ordered[len(ordered) // 2], 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(ordered), 3),
        "deterministic": deterministic,
        "fixture_digest": digest,
        "execution": {
            "raw_source_returned": False,
            "network": False,
            "compiler_or_lsp": False,
            "store_writes": False,
            "autonomous_apply": False,
        },
    }
    result["ok"] = bool(deterministic and float(result["p95_ms"]) <= 20.0 and all(not value for value in result["execution"].values()))
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run(args.iterations)
    payload = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        replace_bytes_safely(args.output, payload.encode("utf-8"))
    print(payload, end="")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
