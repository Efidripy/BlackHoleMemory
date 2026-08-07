from __future__ import annotations

from pathlib import Path

from blackholememory.resource_limits import QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS


ROOT = Path(__file__).resolve().parents[2]


def test_qdrant_health_probes_use_registry_timeout() -> None:
    assert QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS == 2.0
    for name in ("app.py", "mem0_adapter.py"):
        text = (ROOT / "src" / "blackholememory" / name).read_text(encoding="utf-8")
        assert "QDRANT_HEALTH_HTTP_TIMEOUT_SECONDS" in text
    assert "timeout=1.0" not in (ROOT / "src" / "blackholememory" / "mem0_adapter.py").read_text(encoding="utf-8")
