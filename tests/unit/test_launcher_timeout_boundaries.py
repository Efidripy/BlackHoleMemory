from __future__ import annotations

import math
import sys
from pathlib import Path

import pytest

SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

# ruff: noqa: E402
import bhm_launcher  # noqa: E402
import bhm_launcher_readiness  # noqa: E402

from blackholememory.resource_limits import LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_POLL_SECONDS
from blackholememory.resource_limits import LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS
from blackholememory.resource_limits import LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS


def test_launcher_transport_defaults_are_registry_backed() -> None:
    launcher_source = Path(bhm_launcher.__file__).read_text(encoding="utf-8")
    readiness_source = Path(bhm_launcher_readiness.__file__).read_text(encoding="utf-8")
    assert "timeout: float = 2.0" not in launcher_source
    assert "timeout: float = 1.0" not in launcher_source
    assert "timeout=3.0" not in launcher_source
    assert "timeout: float = 2.0" not in readiness_source
    assert bhm_launcher.http_status.__defaults__ == (LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS,)
    assert bhm_launcher.tcp_status.__defaults__ == (LAUNCHER_TCP_PROBE_TIMEOUT_SECONDS, None)
    assert bhm_launcher.SERVICE_READINESS_TIMEOUT_SECONDS == LAUNCHER_SERVICE_READINESS_TIMEOUT_SECONDS
    assert bhm_launcher.SERVICE_READINESS_POLL_SECONDS == LAUNCHER_SERVICE_READINESS_POLL_SECONDS
    assert LAUNCHER_REMOTE_HTTP_TIMEOUT_SECONDS == 3.0


def test_launcher_readiness_inputs_are_finite_and_registry_bounded() -> None:
    assert bhm_launcher_readiness.bound_timeout(999, maximum=2.0) == 2.0
    assert bhm_launcher_readiness.bound_timeout(-1, maximum=2.0, minimum=0.01) == 0.01
    with pytest.raises(ValueError, match="finite"):
        bhm_launcher_readiness.bound_timeout(math.inf, maximum=2.0)
