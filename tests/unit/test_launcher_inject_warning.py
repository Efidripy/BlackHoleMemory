from __future__ import annotations

from types import SimpleNamespace

import psutil
import pytest

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import bhm_launcher as launcher  # noqa: E402


def _procs(*items):
    return lambda attrs=None: [SimpleNamespace(info={"name": name, "exe": exe}) for name, exe in items]


def test_claude_desktop_running_detects_the_app_but_not_its_bundled_cli_or_others(monkeypatch):
    app = r"C:\Program Files\WindowsApps\Claude_1\app\Claude.exe"
    cli = r"C:\Users\u\AppData\Roaming\Claude\claude-code\2.1.304\x\claude.exe"
    monkeypatch.setattr(psutil, "process_iter", _procs(("Claude.exe", app)))
    assert launcher.claude_desktop_running() is True
    monkeypatch.setattr(psutil, "process_iter", _procs(("claude.exe", cli), ("chrome.exe", None)))
    assert launcher.claude_desktop_running() is False
    monkeypatch.setattr(psutil, "process_iter", _procs(("claude.exe", None)))
    assert launcher.claude_desktop_running() is True


def test_claude_desktop_running_is_false_on_psutil_errors(monkeypatch):
    def boom(attrs=None):
        raise psutil.Error("denied")

    monkeypatch.setattr(psutil, "process_iter", boom)
    assert launcher.claude_desktop_running() is False


class _FakeBox:
    class StandardButton:
        Yes = 16384
        No = 65536

    answer = None
    asked = 0

    @classmethod
    def question(cls, *args):
        cls.asked += 1
        return cls.answer


@pytest.mark.parametrize(
    ("running", "answer_yes", "expected", "asked"),
    [(False, False, True, 0), (True, True, True, 1), (True, False, False, 1)],
)
def test_confirm_inject_asks_only_when_claude_is_running(monkeypatch, running, answer_yes, expected, asked):
    _FakeBox.asked = 0
    _FakeBox.answer = _FakeBox.StandardButton.Yes if answer_yes else _FakeBox.StandardButton.No
    monkeypatch.setattr(launcher, "QMessageBox", _FakeBox, raising=False)
    monkeypatch.setattr(launcher, "claude_desktop_running", lambda: running)
    assert launcher.confirm_inject_with_claude_closed(object()) is expected
    assert _FakeBox.asked == asked


def test_prompt_and_restart_hint_are_user_facing_russian_text():
    assert "Claude Desktop" in launcher.CLAUDE_DESKTOP_RUNNING_PROMPT and "трее" in launcher.CLAUDE_DESKTOP_RUNNING_PROMPT
    assert "Перезапустите" in launcher.CLAUDE_DESKTOP_RESTART_HINT
