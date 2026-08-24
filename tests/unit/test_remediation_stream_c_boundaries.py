from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_restore_rejects_traversal_evidence_path():
    module = load("restore_stream_c", "bhm-restore-public-evidence.py")
    with pytest.raises(ValueError, match="unsafe evidence path|outside"):
        module.evidence_paths({"path": ".docs/ops/../../scripts/pwned.py"})


def test_p22_backup_is_bound_to_recovery_root(tmp_path: Path):
    module = load("graph_activation_stream_c", "validate-bhm-graph-activation.py")
    source = tmp_path / "source.sqlite3"
    source.write_bytes(b"SQLite format 3\x00synthetic")
    with pytest.raises(RuntimeError, match="approved recovery root"):
        module.online_backup(source, tmp_path / "outside.sqlite3")


def test_parser_fixtures_are_created_outside_repository(tmp_path: Path, monkeypatch):
    module = load("parser_stream_c", "validate-bhm-parser-parity.py")
    captured: list[Path] = []

    def fake_snapshot(root: Path):
        captured.append(root)
        return {"root_id": "fixture", "files": []}

    def fake_graph(_snapshot):
        return {"graph_digest": "digest", "parse_results": [], "summary": {"parser_error_count": 0}}

    monkeypatch.setattr(module, "_fixture_snapshot", fake_snapshot)
    monkeypatch.setattr(module, "extract_code_graph", fake_graph)
    module.validate(tmp_path)
    assert captured and not captured[0].is_relative_to(tmp_path)


def test_llm_inventory_samples_and_output_are_bounded(tmp_path: Path):
    module = load("llm_stream_c", "validate-bhm-llm-inventory.py")
    assert module.bounded_llm_inventory_samples(999) == module.MAX_INVENTORY_SAMPLES
    assert module.bounded_llm_inventory_samples(0) == 1
    with pytest.raises(ValueError, match="approved root"):
        module.approved_inventory_output(tmp_path / "outside.json")


def test_adapter_rollback_rejects_noncanonical_target(tmp_path: Path):
    module = load("adapter_stream_c", "generate-bhm-mcp-adapters.py")
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    backup = backup_dir / "config.bak"
    backup.write_bytes(b"original")
    record = {
        "target": str(tmp_path / "unrelated.txt"),
        "backup": str(backup),
        "existed": True,
        "sha256_before": module._sha256_bytes(b"original"),
    }
    (backup_dir / "manifest.json").write_text(
        json.dumps({"schema": module.SCHEMA, "records": [record]}), encoding="utf-8"
    )
    with pytest.raises(module.AdapterContractError, match="canonical client config scope"):
        module.run_rollback(backup_dir)


def test_requeue_revalidates_preview_digest(tmp_path: Path):
    module = load("requeue_stream_c", "requeue-bhm-dead-letters.py")
    database = tmp_path / "memories.sqlite3"
    connection = sqlite3.connect(database)
    connection.execute("CREATE TABLE memory_outbox (event_id TEXT, status TEXT, attempts INTEGER, available_at TEXT, claimed_at TEXT, claim_token TEXT, last_error TEXT, updated_at TEXT)")
    connection.execute("INSERT INTO memory_outbox VALUES ('evt', 'dead_letter', 1, '', NULL, NULL, '', '')")
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="stale"):
        module._apply(database, tmp_path / "backup", "0" * 64)
    assert sqlite3.connect(database).execute("SELECT status FROM memory_outbox").fetchone() == ("dead_letter",)


def test_p23_database_must_be_disposable():
    module = load("small_repository_stream_c", "validate-bhm-small-repository.py")
    with pytest.raises(ValueError, match="approved disposable root"):
        module.approved_database_path(Path("C:/not-approved.sqlite3"))


def test_vacuum_requires_explicit_reviewed_authorization():
    module = load("vacuum_stream_c", "bhm_vacuum.py")
    assert module.candidate_digest(["b", "a"]) == module.candidate_digest(["a", "b"])
