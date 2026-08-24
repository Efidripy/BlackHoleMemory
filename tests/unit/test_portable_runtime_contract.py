from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "initialize-bhm-runtime.py"


def load_initializer():
    spec = importlib.util.spec_from_file_location("bhm_initialize_runtime", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_runtime_initializer_creates_idempotent_wal_schema(tmp_path):
    initializer = load_initializer()
    database = tmp_path / "runtime" / "live-memory" / "memories.sqlite3"

    first = initializer.initialize_runtime_database(database)
    second = initializer.initialize_runtime_database(database)

    assert first["ok"] is True
    assert first["created"] is True
    assert second["ok"] is True
    assert second["created"] is False
    assert second["journal_mode"] == "wal"
    assert second["quick_check"] == "ok"


def test_runtime_initializer_verify_only_fails_closed_for_missing_target(tmp_path):
    initializer = load_initializer()
    report = initializer.initialize_runtime_database(
        tmp_path / "missing.sqlite3",
        verify_only=True,
    )

    assert report["ok"] is False
    assert report["action"] == "verify"
    assert report["created"] is False


def test_runtime_initializer_accepts_freshness_schema_v2(tmp_path):
    initializer = load_initializer()
    database = tmp_path / "runtime" / "live-memory" / "memories.sqlite3"
    initializer.initialize_runtime_database(database)

    import sqlite3

    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE freshness_candidates (
                candidate_id TEXT PRIMARY KEY,
                project TEXT NOT NULL,
                memory_id TEXT NOT NULL,
                reason_code TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE freshness_candidate_events (
                event_id TEXT PRIMARY KEY,
                candidate_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );
            CREATE TABLE freshness_scan_state (
                project TEXT PRIMARY KEY,
                scan_id TEXT NOT NULL,
                as_of TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            PRAGMA user_version = 2;
            """
        )

    report = initializer.initialize_runtime_database(database, verify_only=True)

    assert report["ok"] is True
    assert report["schema_version"] == 2


def test_portable_validator_requires_authoritative_contract():
    text = (REPO_ROOT / "scripts" / "validate-bhm-portable-install.ps1").read_text(encoding="utf-8")
    for marker in (
        "verify-release-build.py",
        "initialize-bhm-runtime.py",
        "BHM_MEMORY_STORE_MODE",
        "sqlite-authoritative",
        "health/cutover",
        "health/slo",
        "BHM_PROVIDER_WARMUP_DISABLED",
        "BHM_CALLER_TOKEN",
        "X-BHM-Caller-Surface",
        "-Headers $callerHeaders",
        "callerTokenWasPresent",
        "Remove-Item Env:BHM_CALLER_TOKEN",
        "PYTHONPATH",
        "checkout_present",
        "ExpectedSourceRevision",
        "expected-source-revision",
    ):
        assert marker in text
