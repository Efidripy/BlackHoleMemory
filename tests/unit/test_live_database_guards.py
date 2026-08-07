from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("name", "filename"),
    (("bhm_code_graph", "bhm-code-graph.py"), ("bhm_conventions", "bhm-conventions.py")),
)
def test_live_database_guard_detects_hardlink_alias(tmp_path: Path, name: str, filename: str) -> None:
    module = _load(name, filename)
    canonical = tmp_path / "memories.sqlite3"
    canonical.write_bytes(b"sqlite-fixture")
    alias = tmp_path / "alias.sqlite3"
    try:
        alias.hardlink_to(canonical)
    except (OSError, NotImplementedError):
        pytest.skip("hardlinks are unavailable on this Windows host")

    module.DEFAULT_DATABASE = canonical
    assert module._is_default_database(alias) is True
