from __future__ import annotations

from pathlib import Path

import pytest

from blackholememory.tools import agent_boundary
from blackholememory.tools.code_ast import ASTCodeManager


def test_sensitive_file_name_is_rejected_before_read(tmp_path: Path) -> None:
    secret = tmp_path / ".env"
    secret.write_text("TOKEN=must-not-read", encoding="utf-8")

    with pytest.raises(PermissionError, match="sensitive path"):
        agent_boundary.resolve_agent_path(
            str(secret),
            allowed_roots=(tmp_path,),
            include_default_roots=False,
        )


def test_path_outside_explicit_root_is_rejected(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside.py"
    allowed.mkdir()
    outside.write_text("print('outside')", encoding="utf-8")

    with pytest.raises(PermissionError, match="outside approved"):
        agent_boundary.resolve_agent_path(
            str(outside),
            allowed_roots=(allowed,),
            include_default_roots=False,
        )


def test_symlink_path_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.py"
    link = tmp_path / "linked.py"
    target.write_text("print('safe')", encoding="utf-8")
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PermissionError, match="symlink"):
        agent_boundary.resolve_agent_path(
            str(link),
            allowed_roots=(tmp_path,),
            include_default_roots=False,
        )


def test_ast_manager_restricts_reads_to_explicit_roots(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    source = allowed / "sample.py"
    source.write_text("def sample():\n    return 1\n", encoding="utf-8")
    manager = ASTCodeManager(
        allowed_roots=(allowed,),
        restrict_to_allowed_roots=True,
    )

    assert "def sample():" in manager.get_file_outline(str(source))


def test_stable_reader_returns_bytes_and_enforces_size(tmp_path: Path) -> None:
    source = tmp_path / "sample.py"
    source.write_bytes(b"print('safe')\n")

    assert agent_boundary.read_agent_bytes(source, max_bytes=64) == b"print('safe')\n"
    with pytest.raises(ValueError, match="too large"):
        agent_boundary.read_agent_bytes(source, max_bytes=4)


def test_image_magic_and_vision_endpoint_are_fail_closed(monkeypatch, tmp_path: Path) -> None:
    fake_png = tmp_path / "fake.png"
    fake_png.write_bytes(b"not a png")
    real_png = tmp_path / "real.png"
    real_png.write_bytes(b"\x89PNG\r\n\x1a\nfixture")

    assert agent_boundary.image_magic_matches(fake_png, fake_png.read_bytes()) is False
    assert agent_boundary.image_magic_matches(real_png, real_png.read_bytes()) is True
    assert agent_boundary.vision_endpoint_allowed("http://127.0.0.1:1234/v1") is True
    assert agent_boundary.vision_endpoint_allowed("https://example.invalid/v1") is False

    monkeypatch.setenv("BHM_AGENT_ALLOWED_VISION_HOSTS", "vision.example")
    assert agent_boundary.vision_endpoint_allowed("https://vision.example/v1") is True
