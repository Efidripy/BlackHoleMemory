from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest

from blackholememory.filesystem_boundaries import replace_bytes_safely


REPO_ROOT = Path(__file__).resolve().parents[2]
REPORT_SCRIPTS = (
    "bhm-factories.py",
    "bhm-human-ui.py",
    "bhm-llm-code-fabric.py",
    "bhm-memory-graph.py",
    "bhm-migration.py",
    "bhm-product-value.py",
    "bhm-repository-index.py",
    "bhm-security-trust-boundary.py",
    "bhm-session-capture.py",
    "bhm-task-graph.py",
    "bhm-unified-context.py",
    "bhm-unified-mcp.py",
)


def _load_script(filename: str) -> ModuleType:
    path = REPO_ROOT / "scripts" / filename
    module_name = f"bhm_internal_report_writer_{path.stem.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_report(module: ModuleType, target: Path) -> None:
    if hasattr(module, "_write_report"):
        if module.__name__.endswith("repository_index"):
            module._write_report(target, {"ok": True})
        else:
            module._write_report(target, '{"ok": true}')
        return
    module._emit({"ok": True}, str(target))


@pytest.mark.parametrize("filename", REPORT_SCRIPTS)
def test_internal_report_cli_uses_shared_boundary_writer(filename: str) -> None:
    source = (REPO_ROOT / "scripts" / filename).read_text(encoding="utf-8")
    assert "replace_bytes_safely" in source
    assert ".write_text(" not in source
    assert "path.parent.mkdir" not in source


@pytest.mark.parametrize("filename", REPORT_SCRIPTS)
def test_internal_report_cli_writes_json_without_runtime_mutation(filename: str, tmp_path: Path) -> None:
    target = tmp_path / "nested" / "report.json"
    _write_report(_load_script(filename), target)
    assert json.loads(target.read_text(encoding="utf-8")) == {"ok": True}


@pytest.mark.parametrize("filename", REPORT_SCRIPTS)
def test_internal_report_cli_rejects_hardlink_target(filename: str, tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    try:
        target.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        _write_report(_load_script(filename), target)
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


@pytest.mark.parametrize("filename", REPORT_SCRIPTS)
def test_internal_report_cli_rejects_reparse_parent(filename: str, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "reports"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"directory symlinks unavailable: {exc}")

    target = linked_parent / "report.json"
    with pytest.raises(OSError, match="symlink|junction|reparse"):
        _write_report(_load_script(filename), target)
    assert not (outside / "report.json").exists()


def test_shared_writer_rejects_linked_target_before_replacement(tmp_path: Path) -> None:
    target = tmp_path / "report.json"
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_bytes(b"sentinel")
    try:
        target.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        replace_bytes_safely(target, b"replacement")
    assert sentinel.read_bytes() == b"sentinel"
