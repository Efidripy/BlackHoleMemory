from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_wl300_validator_is_fixture_only_and_green() -> None:
    script = Path(__file__).parents[2] / "scripts" / "validate-bhm-wl300-1-memory-class.py"
    completed = subprocess.run(
        [sys.executable, str(script)],
        check=True,
        capture_output=True,
        text=True,
    )
    report = json.loads(completed.stdout)
    assert report["schema_version"] == "bhm.wl300.1.memory-class-validation.v1"
    assert report["writes_live_state"] is False
    assert report["sqlite_fixture_only"] is True
    assert report["checks"]["apply_ok"] is True
    assert report["checks"]["post_commit_confirmation"]["ok"] is True
