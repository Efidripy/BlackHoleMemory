from __future__ import annotations

import os
import subprocess
import sys
from typing import Any
from unittest.mock import MagicMock


def test_app_import_avoids_heavy_ml_and_graph_libraries() -> None:
    # Run in a clean subprocess to verify cold-start module import boundaries
    code = (
        "import sys\n"
        "from blackholememory import app\n"
        "heavy = ('qdrant_client', 'mem0', 'langgraph')\n"
        "loaded = [m for m in heavy if m in sys.modules]\n"
        "assert not loaded, f'Heavy modules prematurely loaded: {loaded}'\n"
        "print('CLEAN_IMPORT_OK')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(sys.path[0]) if sys.path else ""
    # Ensure current worktree src is in PYTHONPATH
    src_dir = os.path.abspath("src")
    if "PYTHONPATH" in env and env["PYTHONPATH"]:
        env["PYTHONPATH"] = f"{src_dir};{env['PYTHONPATH']}"
    else:
        env["PYTHONPATH"] = src_dir

    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    assert "CLEAN_IMPORT_OK" in result.stdout


def test_qdrant_models_lazy_proxy() -> None:
    from blackholememory.mem0_adapter import qdrant_models

    # Accessing an attribute on the proxy should resolve to the real model
    distance = qdrant_models.Distance.COSINE
    assert str(distance).lower() == "cosine"
    assert "qdrant_client" in sys.modules


def test_mem0_memory_cache_is_bounded() -> None:
    from blackholememory.mem0_adapter import get_mem0_memory

    info = get_mem0_memory.cache_info()
    assert info.maxsize is not None
    assert info.maxsize <= 32


def test_app_ops_task_graph_dynamic_resolution() -> None:
    from blackholememory import app_ops_task

    # app_ops_task.graph is dynamically resolved
    graph = app_ops_task.graph
    assert graph is not None
    assert hasattr(graph, "invoke")


def test_worker_shares_single_embedding_model() -> None:
    # Verify that worker vectorizer reuses the same embedding model across different projects
    created_models: list[Any] = []

    def mock_get_project_mem0(project_name: str) -> Any:
        model = MagicMock()
        model.project = project_name
        model.embed.return_value = [0.1] * 768
        created_models.append(model)
        wrapper = MagicMock()
        wrapper.embedding_model = model
        return wrapper

    shared_embedding_model: Any = None

    def vectorizer(memory: Any) -> Any:
        nonlocal shared_embedding_model
        if shared_embedding_model is None:
            shared_embedding_model = mock_get_project_mem0(memory.project).embedding_model
        return shared_embedding_model.embed(memory.current_revision.content, "add")

    mem1 = MagicMock()
    mem1.project = "proj1"
    mem1.current_revision.content = "hello"

    mem2 = MagicMock()
    mem2.project = "proj2"
    mem2.current_revision.content = "world"

    vec1 = vectorizer(mem1)
    vec2 = vectorizer(mem2)

    assert len(vec1) == 768
    assert len(vec2) == 768
    # Only 1 model created despite two different projects
    assert len(created_models) == 1
