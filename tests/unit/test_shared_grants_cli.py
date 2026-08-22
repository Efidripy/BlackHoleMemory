from __future__ import annotations

import json
import subprocess
import sys


def test_shared_grants_cli_is_dry_run_first(tmp_path) -> None:
    command = [
        sys.executable,
        "scripts/manage-bhm-shared-grants.py",
        "--runtime-dir",
        str(tmp_path),
        "--grant",
        "--grant-id",
        "grant-cli",
        "--project",
        "blackholememory",
        "--owner-id",
        "owner",
        "--grantee-id",
        "agent",
        "--operation",
        "read",
        "--issued-at",
        "2026-08-23T12:00:00Z",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=True)
    payload = json.loads(result.stdout)
    assert payload["applied"] is False
    assert payload["shared_read_enabled"] is False
    assert payload["shared_write_enabled"] is False
    assert not (tmp_path / "live-memory" / "memories.sqlite3").exists()


def test_shared_grants_cli_applies_then_revokes_only_immutable_artifacts(tmp_path) -> None:
    subprocess.run(
        [sys.executable, "scripts/initialize-bhm-runtime.py", "--runtime-dir", str(tmp_path)],
        capture_output=True, text=True, check=True,
    )
    common = [sys.executable, "scripts/manage-bhm-shared-grants.py", "--runtime-dir", str(tmp_path)]
    grant = subprocess.run(
        common + [
            "--grant", "--grant-id", "grant-cli", "--project", "blackholememory",
            "--owner-id", "owner", "--grantee-id", "agent", "--operation", "read",
            "--issued-at", "2026-08-23T12:00:00Z", "--apply",
        ],
        capture_output=True, text=True, check=True,
    )
    granted = json.loads(grant.stdout)
    assert granted["applied"] is True
    assert granted["inserted"] is True
    revoke = subprocess.run(
        common + [
            "--revoke", "--grant-id", "grant-cli", "--project", "blackholememory",
            "--revoked-at", "2026-08-23T12:01:00Z", "--revocation-receipt-digest", "a" * 64,
            "--apply",
        ],
        capture_output=True, text=True, check=True,
    )
    revoked = json.loads(revoke.stdout)
    assert revoked["action"] == "revoke"
    assert revoked["inserted"] is True
    assert revoked["shared_read_enabled"] is False
    assert revoked["shared_write_enabled"] is False
