from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
import pytest


ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts" / "verify-release-source-tree.py"
    spec = importlib.util.spec_from_file_location("bhm_test_release_source_tree", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _fixture(root: Path) -> Path:
    (root / "src/blackholememory").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "src/blackholememory/app.py").write_text("print('stable')\n", encoding="utf-8")
    (root / "config/version-manifest.json").write_text("{}\n", encoding="utf-8")
    for name in ("pyproject.toml", "uv.lock", "LICENSE"):
        (root / name).write_text(f"{name}\n", encoding="utf-8")
    return root


def _git_blobs(root: Path, paths: set[str]) -> dict[str, bytes]:
    return {relative: (root / relative).read_bytes() for relative in paths}


def test_source_tree_verifier_accepts_matching_staged_snapshot(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    staged = _fixture(tmp_path / "staged")
    monkeypatch.setattr(
        module,
        "git_value",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "b" * 40 if args == ("rev-parse", "HEAD^{tree}") else "",
    )
    monkeypatch.setattr(module, "git_tracked_paths", lambda _root: {
        "src/blackholememory/app.py",
        "config/version-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    })
    monkeypatch.setattr(module, "git_blob_snapshot", lambda root, paths: _git_blobs(source, paths))

    result = module.verify(
        source_root=source,
        release_root=staged,
        expected_revision="a" * 40,
        expected_tree="b" * 40,
    )

    assert result["ok"] is True
    assert result["source_snapshot_sha256"] == result["staged_snapshot_sha256"]


def test_source_tree_git_callers_expose_distinct_registry_bounds(monkeypatch, tmp_path):
    module = _module()
    calls: list[dict[str, object]] = []

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        calls.append(kwargs)
        if "ls-tree" in command:
            return Result(b"src/blackholememory/app.py\0")
        if "archive" in command:
            return Result(b"not-a-tar")
        return Result("head\n")

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    root = tmp_path / "source"
    root.mkdir()

    assert module.git_value(root, "rev-parse", "HEAD") == "head"
    assert module.git_tracked_paths(root) == {"src/blackholememory/app.py"}
    assert module.git_blob_snapshot(root, {"src/blackholememory/app.py"}) is None
    assert [call["timeout"] for call in calls] == [
        module.RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS,
        module.RELEASE_SOURCE_TREE_GIT_TIMEOUT_SECONDS,
        module.RELEASE_SOURCE_TREE_ARCHIVE_TIMEOUT_SECONDS,
    ]


def test_source_tree_verifier_rejects_staged_tamper(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    staged = _fixture(tmp_path / "staged")
    (staged / "src/blackholememory/app.py").write_text("print('tampered')\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "git_value",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "b" * 40 if args == ("rev-parse", "HEAD^{tree}") else "",
    )
    monkeypatch.setattr(module, "git_tracked_paths", lambda _root: {
        "src/blackholememory/app.py",
        "config/version-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    })
    monkeypatch.setattr(module, "git_blob_snapshot", lambda root, paths: _git_blobs(source, paths))

    result = module.verify(
        source_root=source,
        release_root=staged,
        expected_revision="a" * 40,
        expected_tree="b" * 40,
    )

    assert result["ok"] is False
    assert "staged source file differs: src/blackholememory/app.py" in result["failures"]


def test_source_tree_verifier_rejects_linked_source_file(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    staged = _fixture(tmp_path / "staged")
    outside = tmp_path / "outside.py"
    outside.write_text("print('outside')\n", encoding="utf-8")
    linked = source / "src/blackholememory/linked.py"
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("file symlinks unavailable on this Windows host")
    monkeypatch.setattr(
        module,
        "git_value",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "b" * 40 if args == ("rev-parse", "HEAD^{tree}") else "",
    )
    monkeypatch.setattr(module, "git_tracked_paths", lambda _root: {
        "src/blackholememory/app.py",
        "config/version-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    })
    monkeypatch.setattr(module, "git_blob_snapshot", lambda root, paths: _git_blobs(source, paths))

    result = module.verify(source_root=source, release_root=staged, expected_revision="a" * 40, expected_tree="b" * 40)

    assert result["ok"] is False
    assert any("source path contains symlink" in item for item in result["failures"])


def test_source_tree_verifier_rejects_linked_source_root_before_git(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    staged = _fixture(tmp_path / "staged")
    linked_root = tmp_path / "linked-source"
    try:
        linked_root.symlink_to(source, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this Windows host")

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("Git must not run before root admission")

    monkeypatch.setattr(module, "git_value", unexpected_git)
    result = module.verify(
        source_root=linked_root,
        release_root=staged,
        expected_revision="a" * 40,
        expected_tree="b" * 40,
    )

    assert result["ok"] is False
    assert any("source root crosses unsafe filesystem boundary" in item for item in result["failures"])


def test_source_tree_verifier_rejects_linked_release_root_before_git(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    staged = _fixture(tmp_path / "staged")
    linked_root = tmp_path / "linked-staged"
    try:
        linked_root.symlink_to(staged, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this Windows host")

    def unexpected_git(*_args, **_kwargs):
        raise AssertionError("Git must not run before root admission")

    monkeypatch.setattr(module, "git_value", unexpected_git)
    result = module.verify(
        source_root=source,
        release_root=linked_root,
        expected_revision="a" * 40,
        expected_tree="b" * 40,
    )

    assert result["ok"] is False
    assert any("staged root crosses unsafe filesystem boundary" in item for item in result["failures"])


def test_source_tree_verifier_rejects_ignored_or_untracked_source_file(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    staged = _fixture(tmp_path / "staged")
    (source / "src/blackholememory/ignored.py").write_text("print('ignored')\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "git_value",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "b" * 40 if args == ("rev-parse", "HEAD^{tree}") else "",
    )
    monkeypatch.setattr(module, "git_tracked_paths", lambda _root: {
        "src/blackholememory/app.py",
        "config/version-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    })
    monkeypatch.setattr(module, "git_blob_snapshot", lambda root, paths: _git_blobs(source, paths))

    result = module.verify(source_root=source, release_root=staged, expected_revision="a" * 40, expected_tree="b" * 40)

    assert result["ok"] is False
    assert any("non-tracked or out-of-scope" in item for item in result["failures"])


def test_source_tree_verifier_allows_launcher_as_generated_root_artifact(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    staged = _fixture(tmp_path / "staged")
    (staged / "BHM_Launcher.exe").write_bytes(b"launcher")
    monkeypatch.setattr(
        module,
        "git_value",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "b" * 40 if args == ("rev-parse", "HEAD^{tree}") else "",
    )
    monkeypatch.setattr(module, "git_tracked_paths", lambda _root: {
        "src/blackholememory/app.py",
        "config/version-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    })
    monkeypatch.setattr(module, "git_blob_snapshot", lambda root, paths: _git_blobs(source, paths))

    result = module.verify(source_root=source, release_root=staged, expected_revision="a" * 40, expected_tree="b" * 40)

    assert result["ok"] is True


def test_source_only_preflight_rejects_non_tracked_source(tmp_path, monkeypatch):
    module = _module()
    source = _fixture(tmp_path / "source")
    (source / "src/blackholememory/ignored.py").write_text("print('ignored')\n", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "git_value",
        lambda _root, *args: "a" * 40 if args == ("rev-parse", "HEAD") else "b" * 40 if args == ("rev-parse", "HEAD^{tree}") else "",
    )
    monkeypatch.setattr(module, "git_tracked_paths", lambda _root: {
        "src/blackholememory/app.py",
        "config/version-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    })
    monkeypatch.setattr(module, "git_blob_snapshot", lambda root, paths: _git_blobs(source, paths))

    result = module.verify_source_only(source_root=source, expected_revision="a" * 40, expected_tree="b" * 40)

    assert result["ok"] is False
    assert any("non-tracked or out-of-scope" in item for item in result["failures"])
