from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts" / "validate-public-tree.py"
    spec = importlib.util.spec_from_file_location("bhm_test_public_tree_script_manifest", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode


def _write_manifest(root: Path, entries: list[dict[str, object]]) -> None:
    config = root / "config"
    config.mkdir(parents=True)
    (config / "public-script-manifest.json").write_text(
        json.dumps({"schema_version": "bhm.public-script-manifest.v1", "entries": entries}),
        encoding="utf-8",
    )


def test_public_script_manifest_requires_exact_tracked_coverage(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    _write_manifest(
        tmp_path,
        [{"path": "scripts/allowed.py", "role": "runtime", "release": True}],
    )

    monkeypatch.setattr(module, "_run_git", lambda *_args: _Result("scripts/allowed.py\nscripts/unlisted.py\n"))

    result = module.validate_public_script_manifest(tmp_path)

    assert result["tracked"] == 2
    assert result["listed"] == 1
    assert result["failures"] == ["tracked script absent from public script manifest: scripts/unlisted.py"]


def test_public_script_manifest_rejects_untracked_duplicate_and_invalid_entries(monkeypatch, tmp_path: Path) -> None:
    module = _module()
    _write_manifest(
        tmp_path,
        [
            {"path": "scripts/allowed.py", "role": "runtime", "release": True},
            {"path": "scripts/allowed.py", "role": "", "release": False},
            {"path": "scripts/extra.ps1", "role": "operator", "release": "no"},
        ],
    )

    monkeypatch.setattr(module, "_run_git", lambda *_args: _Result("scripts/allowed.py\n"))

    result = module.validate_public_script_manifest(tmp_path)

    assert result["tracked"] == 1
    assert result["listed"] == 2
    assert "public script manifest has duplicate path: scripts/allowed.py" in result["failures"]
    assert "public script manifest has no role: scripts/allowed.py" in result["failures"]
    assert "public script manifest has invalid release flag: scripts/extra.ps1" in result["failures"]
    assert "public script manifest lists untracked script: scripts/extra.ps1" in result["failures"]


def test_current_public_script_manifest_is_exactly_classified() -> None:
    module = _module()

    result = module.validate_public_script_manifest(ROOT)

    assert result["failures"] == []
    assert result["tracked"] == result["listed"] == 232
