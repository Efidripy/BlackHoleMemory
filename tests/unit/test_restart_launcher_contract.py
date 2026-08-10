from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_restart_launcher_does_not_flash_a_minimized_powershell_console():
    source = (REPO_ROOT / "src" / "blackholememory" / "app.py").read_text(encoding="utf-8")

    section = source[source.index("def _spawn_detached_restart_launcher"):source.index("def _register_infra_pid")]
    assert '"cmd.exe"' in section
    assert '"powershell.exe"' in section
    assert '"/b"' in section
    assert '"/min"' not in section
    assert "_WINDOWS_CREATE_NO_WINDOW" in source
    assert "subprocess.STARTF_USESHOWWINDOW" in source
    assert "subprocess.SW_HIDE" in source
