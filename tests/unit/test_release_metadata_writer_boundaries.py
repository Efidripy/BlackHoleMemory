from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _load_script(filename: str, module_name: str):
    path = ROOT / "scripts" / filename
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


manifest_writer = _load_script("build-release-manifest.py", "bhm_test_release_manifest_writer")
inputs_writer = _load_script("capture-release-build-inputs.py", "bhm_test_release_inputs_writer")


def _manifest_root(root: Path) -> None:
    (root / "config").mkdir(parents=True)
    (root / "config" / "version-manifest.json").write_text(
        json.dumps({"release_version": "1.8.1"}), encoding="utf-8"
    )


def _inputs_root(root: Path) -> None:
    (root / "uv.lock").write_text("version = 1\n", encoding="utf-8")


def test_release_manifest_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    _manifest_root(tmp_path)
    sentinel = tmp_path / "sentinel.json"
    sentinel.write_text("sentinel", encoding="utf-8")
    target = tmp_path / "release-manifest.json"
    try:
        target.hardlink_to(sentinel)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    manifest = manifest_writer.build_manifest(tmp_path, "v1.8.1")
    with pytest.raises(OSError, match="hardlink"):
        manifest_writer.write_manifest(tmp_path, manifest)
    assert sentinel.read_text(encoding="utf-8") == "sentinel"


def test_release_inputs_writer_rejects_output_outside_root(tmp_path: Path) -> None:
    _inputs_root(tmp_path)
    output = tmp_path.parent / "outside-build-inputs.json"
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(inputs_writer.metadata, "version", lambda _name: "test")
    try:
        with pytest.raises(SystemExit, match="escapes release root"):
            inputs_writer.capture(
                tmp_path,
                output,
                source_revision="a" * 40,
                source_snapshot_sha256="b" * 64,
                launcher=None,
                uv_version="test",
            )
    finally:
        monkeypatch.undo()
    assert not output.exists()


def test_release_inputs_writer_writes_json_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _inputs_root(tmp_path)
    monkeypatch.setattr(inputs_writer.metadata, "version", lambda _name: "test")
    output = tmp_path / "build-inputs.json"

    receipt = inputs_writer.capture(
        tmp_path,
        output,
        source_revision="a" * 40,
        source_snapshot_sha256="b" * 64,
        launcher=None,
        uv_version="test",
    )

    assert output.is_file()
    assert json.loads(output.read_text(encoding="utf-8")) == receipt
