"""Deterministic WI-176 benchmark for the clean-room inventory parser lane."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
from typing import Any

from blackholememory.code_graph import PARSER_REGISTRY
from blackholememory.code_graph import _GraphDraft
from blackholememory.code_graph import _extract_script
from blackholememory.code_graph import _file_node_key
from blackholememory.code_graph import _node
from blackholememory.code_graph import _sha256
from blackholememory.code_graph import _INVENTORY_METADATA_LANGUAGES


def _fixture() -> dict[str, str]:
    return {
        language: f"module Demo\nfn run_{language.replace('-', '_')}() {{}}\nimport dependency\n"
        for language in _INVENTORY_METADATA_LANGUAGES
    }


def _run(fixtures: dict[str, str]) -> dict[str, Any]:
    digest = hashlib.sha256(json.dumps(fixtures, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    draft = _GraphDraft("wi176-root", {"snapshot_id": "wi176-snapshot"})
    for language, content in fixtures.items():
        path = f"fixture.{language}"
        file_key = _file_node_key(draft.root_id, path)
        file_hash = _sha256(content)
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
                end_line=3,
                signature="",
                content_sha256=file_hash,
                parser_version=PARSER_REGISTRY[language]["version"],
            )
        )
        _extract_script(draft, path, file_key, content, file_hash, language)
    parsed = sum(1 for language in fixtures if language in PARSER_REGISTRY)
    return {"fixture_digest": digest, "languages": len(fixtures), "parsed_dispatches": parsed, "nodes": len(draft.nodes), "imports": sum(len(v) for v in draft.imports.values())}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--iterations", type=int, default=1000)
    args = parser.parse_args()
    fixtures = _fixture()
    samples: list[float] = []
    result: dict[str, Any] = {}
    for _ in range(max(1, int(args.iterations))):
        start = time.perf_counter_ns()
        result = _run(fixtures)
        samples.append((time.perf_counter_ns() - start) / 1_000_000)
    samples.sort()
    print(json.dumps({
        "ok": result["parsed_dispatches"] == result["languages"],
        "iterations": len(samples),
        "p50_ms": round(statistics.median(samples), 4),
        "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 4),
        "max_ms": round(max(samples), 4),
        "parser_registry_count": len(PARSER_REGISTRY),
        **result,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
