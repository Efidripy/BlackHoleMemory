from __future__ import annotations

from pathlib import Path

from blackholememory.resource_limits import LOCAL_SOCKET_PROBE_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]
SCRIPTS = (
    ROOT / "scripts" / "bhm_classify_projection_orphans.py",
    ROOT / "scripts" / "bhm_reconcile_projection.py",
    ROOT / "scripts" / "bhm_quarantine_projection_orphans.py",
)


def test_projection_listener_probes_use_registry_timeout() -> None:
    assert LOCAL_SOCKET_PROBE_TIMEOUT_SECONDS == 0.25
    for script in SCRIPTS:
        text = script.read_text(encoding="utf-8")
        assert "LOCAL_SOCKET_PROBE_TIMEOUT_SECONDS" in text
        assert "timeout=0.25" not in text
