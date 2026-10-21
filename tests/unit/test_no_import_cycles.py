from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _cycles() -> list[list[str]]:
    spec = importlib.util.spec_from_file_location("check_import_cycles", ROOT / "scripts" / "check-import-cycles.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    source_root = ROOT / "src"
    package_root = source_root / "blackholememory"
    files = module.find_modules(package_root, source_root)
    graph = module.build_import_graph(files, include_function_level=False)
    return module.tarjan_scc(graph)


def test_developer_agent_and_code_graph_have_no_import_cycles() -> None:
    cycles = _cycles()
    forbidden_prefixes = (
        "blackholememory.agents.developer_agent.",
        "blackholememory.code_graph.",
    )
    forbidden = [cycle for cycle in cycles if any(name.startswith(forbidden_prefixes) for name in cycle)]
    # Cycles outside these packages are intentionally not part of this safety net.
    ALLOWED_CYCLES: list[list[str]] = []
    assert not forbidden, f"import cycles in target packages: {forbidden}; allowed={ALLOWED_CYCLES}"
