from __future__ import annotations

import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def _module():
    path = ROOT / "scripts" / "materialize-release-source.py"
    spec = importlib.util.spec_from_file_location("bhm_test_materialize_release_source", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _archive(*, include_symlink: bool = False) -> bytes:
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:") as archive:
        for name, data in {
            "scripts/bhm_launcher.py": b"launcher\n",
            "src/blackholememory/app.py": b"app\n",
            "config/version-manifest.json": b"{}\n",
            "pyproject.toml": b"[project]\n",
            "uv.lock": b"version = 1\n",
            "LICENSE": b"0BSD\n",
            "README.md": b"ignored by source materializer\n",
        }.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
        if include_symlink:
            info = tarfile.TarInfo("scripts/secret.env")
            info.type = tarfile.SYMTYPE
            info.linkname = "outside"
            archive.addfile(info)
    return payload.getvalue()


def _tree_listing(*, include_symlink: bool = False, mode: str = "100644") -> bytes:
    paths = [
        "scripts/bhm_launcher.py",
        "src/blackholememory/app.py",
        "config/version-manifest.json",
        "pyproject.toml",
        "uv.lock",
        "LICENSE",
    ]
    if include_symlink:
        paths.append("scripts/secret.env")
    rows = [f"{mode} blob {'0' * 40}\t{path}" for path in paths]
    return ("\0".join(rows) + "\0").encode()


def test_materializer_extracts_only_tracked_allowlist_and_binds_digest(tmp_path, monkeypatch):
    module = _module()
    revision = "a" * 40
    tree = "b" * 40
    archive = _archive()

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    timeouts: list[object] = []

    def fake_run(command, **kwargs):
        timeouts.append(kwargs.get("timeout"))
        if command[-2:] == ["rev-parse", "HEAD"]:
            return Result(revision + "\n")
        if command[-2:] == ["rev-parse", "HEAD^{tree}"]:
            return Result(tree + "\n")
        if "status" in command:
            return Result("")
        if "ls-tree" in command:
            return Result(_tree_listing())
        if "archive" in command:
            return Result(archive)
        raise AssertionError(command)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    result = module.materialize(
        repo_root=tmp_path / "repo",
        output_root=tmp_path / "snapshot",
        expected_revision=revision,
        expected_tree=tree,
    )

    assert result["ok"] is True
    assert result["file_count"] == 6
    assert (tmp_path / "snapshot/scripts/bhm_launcher.py").is_file()
    assert not (tmp_path / "snapshot/README.md").exists()
    assert timeouts == [
        module.RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS,
        module.RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS,
        module.RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS,
        module.RELEASE_MATERIALIZE_GIT_TIMEOUT_SECONDS,
        module.RELEASE_ARCHIVE_TIMEOUT_SECONDS,
    ]


def test_materializer_rejects_symlink_source_entry(tmp_path, monkeypatch):
    module = _module()
    revision = "a" * 40
    tree = "b" * 40
    archive = _archive(include_symlink=True)

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return Result(revision + "\n")
        if command[-2:] == ["rev-parse", "HEAD^{tree}"]:
            return Result(tree + "\n")
        if "status" in command:
            return Result("")
        if "ls-tree" in command:
            return Result(_tree_listing(include_symlink=True))
        if "archive" in command:
            return Result(archive)
        raise AssertionError(command)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="non-regular"):
        module.materialize(
            repo_root=tmp_path / "repo",
            output_root=tmp_path / "snapshot",
            expected_revision=revision,
            expected_tree=tree,
        )


def test_materializer_rejects_unsupported_tracked_mode(tmp_path, monkeypatch):
    module = _module()
    revision = "a" * 40
    tree = "b" * 40

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return Result(revision + "\n")
        if command[-2:] == ["rev-parse", "HEAD^{tree}"]:
            return Result(tree + "\n")
        if "status" in command:
            return Result("")
        if "ls-tree" in command:
            return Result(_tree_listing(mode="160000"))
        raise AssertionError(command)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="unsupported entry type"):
        module.materialize(
            repo_root=tmp_path / "repo",
            output_root=tmp_path / "snapshot",
            expected_revision=revision,
            expected_tree=tree,
        )


def test_materializer_cleans_partial_output_on_missing_tracked_entry(tmp_path, monkeypatch):
    module = _module()
    revision = "a" * 40
    tree = "b" * 40
    archive = _archive()

    class Result:
        def __init__(self, stdout):
            self.stdout = stdout

    def fake_run(command, **kwargs):
        if command[-2:] == ["rev-parse", "HEAD"]:
            return Result(revision + "\n")
        if command[-2:] == ["rev-parse", "HEAD^{tree}"]:
            return Result(tree + "\n")
        if "status" in command:
            return Result("")
        if "ls-tree" in command:
            return Result(_tree_listing() + b"100644 blob " + b"0" * 40 + b"\tscripts/missing.py\0")
        if "archive" in command:
            return Result(archive)
        raise AssertionError(command)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(SystemExit, match="missing required files"):
        module.materialize(
            repo_root=tmp_path / "repo",
            output_root=tmp_path / "snapshot",
            expected_revision=revision,
            expected_tree=tree,
        )
    assert not (tmp_path / "snapshot").exists()
    assert not list(tmp_path.glob(".snapshot.partial-*"))


def test_materializer_rejects_reparse_output_parent_before_git_probe(tmp_path, monkeypatch):
    module = _module()
    outside = tmp_path / "outside"
    outside.mkdir()
    linked_parent = tmp_path / "linked-parent"
    linked_parent.symlink_to(outside, target_is_directory=True)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Git must not run before output boundary admission")

    monkeypatch.setattr(module.subprocess, "run", fail_if_called)
    with pytest.raises(OSError, match="symlink|reparse"):
        module.materialize(
            repo_root=tmp_path / "repo",
            output_root=linked_parent / "snapshot",
            expected_revision="a" * 40,
            expected_tree="b" * 40,
        )


def test_materializer_rejects_hardlinked_existing_output_before_git_probe(tmp_path, monkeypatch):
    module = _module()
    source = tmp_path / "source.bin"
    output = tmp_path / "snapshot"
    source.write_bytes(b"existing")
    output.hardlink_to(source)

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("Git must not run before output boundary admission")

    monkeypatch.setattr(module.subprocess, "run", fail_if_called)
    with pytest.raises(OSError, match="hardlink"):
        module.materialize(
            repo_root=tmp_path / "repo",
            output_root=output,
            expected_revision="a" * 40,
            expected_tree="b" * 40,
        )
