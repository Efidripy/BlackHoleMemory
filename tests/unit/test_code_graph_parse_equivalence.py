from __future__ import annotations

import hashlib
from pathlib import Path

from blackholememory.code_graph import extract_code_graph


def _snapshot(root: Path) -> dict[str, object]:
    files: list[dict[str, object]] = []
    contents = {
        "app.py": "def run():\n    return helper()\n\ndef helper():\n    return 1\n",
        "README.md": "# Demo\n\n## Usage\n",
        "tests/test_app.py": "from app import run\n\ndef test_run():\n    assert run() == 1\n",
    }
    for path, content in contents.items():
        payload = content.encode("utf-8")
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_bytes(payload)
        files.append(
            {
                "path": path,
                "language": "markdown" if path.endswith(".md") else "python",
                "file_kind": "test" if path.startswith("tests/") else "source",
                "size_bytes": len(payload),
                "content_sha256": hashlib.sha256(payload).hexdigest(),
            }
        )
    return {
        "root_id": "synthetic-root",
        "root_path": str(root),
        "project": "synthetic",
        "snapshot_id": "synthetic-snapshot",
        "snapshot_digest": "synthetic-digest",
        "graph_input_digest": "synthetic-input",
        "files": files,
    }


def test_parse_result_counts_are_equivalent_and_deterministic(tmp_path: Path) -> None:
    first = extract_code_graph(_snapshot(tmp_path / "repo"))
    second = extract_code_graph(_snapshot(tmp_path / "repo"))

    assert first["graph_digest"] == second["graph_digest"]
    assert first["nodes_digest"] == second["nodes_digest"]
    assert first["edges_digest"] == second["edges_digest"]
    assert first["parse_results"] == second["parse_results"]

    nodes_by_path: dict[str, int] = {}
    for node in first["nodes"]:
        path = str(node.get("path") or "")
        nodes_by_path[path] = nodes_by_path.get(path, 0) + 1
    edge_counts: dict[str, int] = {}
    parse_paths = {str(item["path"]) for item in first["parse_results"]}
    for edge in first["edges"]:
        matched = {
            path
            for ref in (edge.get("evidence") or {}).get("source_refs", [])
            for path in parse_paths
            if str(ref).startswith(f"{path}#")
        }
        for path in matched:
            edge_counts[path] = edge_counts.get(path, 0) + 1
    for item in first["parse_results"]:
        path = str(item["path"])
        assert item["node_count"] == nodes_by_path.get(path, 0)
        assert item["edge_count"] == edge_counts.get(path, 0)
