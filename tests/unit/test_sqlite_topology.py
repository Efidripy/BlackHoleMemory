from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from blackholememory.llm_cache import default_llm_cache_path


REPO_ROOT = Path(__file__).resolve().parents[2]
INITIALIZER = REPO_ROOT / "scripts" / "initialize-bhm-runtime.py"


def load_initializer():
    spec = importlib.util.spec_from_file_location("bhm_initialize_runtime_topology", INITIALIZER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_llm_cache_default_stays_inside_hidden_runtime(monkeypatch):
    monkeypatch.delenv("BHM_LLM_CACHE_PATH", raising=False)

    assert default_llm_cache_path() == REPO_ROOT / ".runtime" / "llm-jobs" / "cache.sqlite3"


def test_runtime_initializer_default_stays_inside_hidden_runtime():
    initializer = load_initializer()

    assert initializer.default_runtime_dir() == REPO_ROOT / ".runtime"
    assert initializer.default_database_path(initializer.default_runtime_dir()) == (
        REPO_ROOT / ".runtime" / "live-memory" / "memories.sqlite3"
    )


def test_explicit_runtime_directory_remains_supported(tmp_path):
    initializer = load_initializer()
    custom_runtime = tmp_path / "runtime"

    assert initializer.default_database_path(custom_runtime) == (
        custom_runtime.resolve() / "live-memory" / "memories.sqlite3"
    )
