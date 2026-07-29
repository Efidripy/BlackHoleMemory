from __future__ import annotations

import ast
from pathlib import Path

from tests.integration.domain_manifest import DOMAIN_NAMES, classify_test


REPO_ROOT = Path(__file__).resolve().parents[2]


def _integration_tests() -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for path in sorted((REPO_ROOT / "tests" / "integration").glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                result.append((path.name, node.name))
    return result


def test_every_integration_test_has_one_domain():
    assignments = [classify_test(file_name, test_name) for file_name, test_name in _integration_tests()]
    # Keep the manifest fail-closed while accounting for the WI-82..WI-198
    # parity, semantic-readiness and security regression surfaces now present
    # in the current test tree.
    assert len(assignments) == 221
    assert set(assignments) == set(DOMAIN_NAMES)
    assert all(assignments.count(domain) > 0 for domain in DOMAIN_NAMES)
