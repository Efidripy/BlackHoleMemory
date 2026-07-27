from __future__ import annotations

import pytest

from blackholememory.tools import scratchpad


def test_isolated_scratchpad_is_namespaced_and_ignores_legacy_override(monkeypatch, tmp_path):
    legacy = tmp_path / "legacy.md"
    monkeypatch.setenv(scratchpad.SCRATCHPAD_ENV_VAR, str(legacy))
    namespace_root = tmp_path / "namespace"
    monkeypatch.setattr(scratchpad, "_namespace_root", lambda: namespace_root)

    result = scratchpad.tool_write_scratchpad(
        "handoff note",
        "developer",
        task_id="task/alpha",
        project="Project One",
        isolated=True,
    )

    assert result == "Scratchpad appended by developer."
    assert not legacy.exists()
    files = list(namespace_root.rglob("*.md"))
    assert files
    assert any("project-one-" in path.parent.name and "task-alpha-" in path.stem for path in files)
    read = scratchpad.tool_read_scratchpad(
        task_id="task/alpha",
        project="Project One",
        isolated=True,
    )
    assert read.startswith(scratchpad.UNTRUSTED_HANDOFF_HEADER)
    assert "handoff note" in read


def test_isolated_scratchpad_requires_task_id(monkeypatch):
    monkeypatch.delenv(scratchpad.SCRATCHPAD_ENV_VAR, raising=False)
    result = scratchpad.tool_write_scratchpad("note", "developer", project="project", isolated=True)
    assert result.startswith(scratchpad.SCRATCHPAD_ERROR_PREFIX)
    assert "requires task_id" in result


def test_isolated_scratchpad_isolated_between_tasks(monkeypatch, tmp_path):
    monkeypatch.setattr(scratchpad, "_namespace_root", lambda: tmp_path / "namespace")
    first = scratchpad.tool_write_scratchpad(
        "task one secret-ish context",
        "developer",
        task_id="task-one",
        project="project",
        isolated=True,
    )
    second = scratchpad.tool_read_scratchpad(task_id="task-two", project="project", isolated=True)
    assert first == "Scratchpad appended by developer."
    assert second == scratchpad.SCRATCHPAD_EMPTY_MESSAGE


def test_scratchpad_rejects_sensitive_operator_path(monkeypatch, tmp_path):
    sensitive = tmp_path / ".env"
    monkeypatch.setenv(scratchpad.SCRATCHPAD_ENV_VAR, str(sensitive))
    result = scratchpad.tool_write_scratchpad("note", "operator")
    assert result.startswith(scratchpad.SCRATCHPAD_ERROR_PREFIX)
    assert "sensitive" in result
    assert not sensitive.exists()


def test_scratchpad_rejects_symlink_operator_path(monkeypatch, tmp_path):
    target = tmp_path / "target.md"
    link = tmp_path / "link.md"
    target.write_text("do not follow", encoding="utf-8")
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this Windows host")

    monkeypatch.setenv(scratchpad.SCRATCHPAD_ENV_VAR, str(link))
    result = scratchpad.tool_write_scratchpad("note", "operator")
    assert result.startswith(scratchpad.SCRATCHPAD_ERROR_PREFIX)
    assert "symlink" in result
    assert target.read_text(encoding="utf-8") == "do not follow"


def test_scratchpad_sanitizes_control_and_bidi_characters(monkeypatch, tmp_path):
    path = tmp_path / "safe.md"
    monkeypatch.setenv(scratchpad.SCRATCHPAD_ENV_VAR, str(path))
    assert scratchpad.tool_write_scratchpad("line\x00\u202ehidden", "operator") == "Scratchpad appended by operator."
    content = path.read_text(encoding="utf-8")
    assert "\x00" not in content
    assert "\u202e" not in content
    read = scratchpad.tool_read_scratchpad()
    assert "\x00" not in read
    assert "\u202e" not in read
    assert scratchpad.UNTRUSTED_HANDOFF_HEADER in read


def test_scratchpad_enforces_size_cap(monkeypatch, tmp_path):
    path = tmp_path / "bounded.md"
    monkeypatch.setenv(scratchpad.SCRATCHPAD_ENV_VAR, str(path))
    path.write_bytes(b"x" * scratchpad.MAX_SCRATCHPAD_BYTES)
    result = scratchpad.tool_write_scratchpad("one more", "operator")
    assert result.startswith(scratchpad.SCRATCHPAD_ERROR_PREFIX)
    assert "size" in result
