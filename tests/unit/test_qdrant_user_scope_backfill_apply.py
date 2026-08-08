from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "apply-bhm-qdrant-user-scope-backfill.py"
spec = importlib.util.spec_from_file_location("bhm_test_user_scope_apply", SCRIPT)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class _FakeQdrant:
    def __init__(self):
        self.set_calls = []
        self.overwrite_calls = []

    def set_payload(self, **kwargs):
        self.set_calls.append(kwargs)

    def overwrite_payload(self, **kwargs):
        self.overwrite_calls.append(kwargs)


def _target():
    payload = {"source_id": "source-1", "content": "legacy body"}
    return {
        "collection": "collection",
        "point_id": "point-1",
        "source_id": "source-1",
        "payload": payload,
        "payload_sha256": module._payload_sha256(payload),
        "add_user_id": True,
        "add_data": True,
        "data_value": "legacy body",
    }


def test_apply_updates_only_missing_fields():
    client = _FakeQdrant()
    assert module._apply(client, [_target()], "user-1") == 1
    assert client.set_calls[0]["payload"] == {"user_id": "user-1", "data": "legacy body"}
    assert client.set_calls[0]["points"] == ["point-1"]


def test_backup_round_trip_and_rollback(tmp_path):
    target = _target()
    plan = {"expected_user_id": "user-1", "summary": {"target_digest": "digest"}}
    manifest = module._write_backup(tmp_path, [target], plan)
    assert manifest["target_count"] == 1
    loaded_manifest, rows = module._load_backup(tmp_path)
    assert loaded_manifest["payload_sha256"] == manifest["payload_sha256"]
    client = _FakeQdrant()
    assert module._rollback(client, rows) == 1
    assert client.overwrite_calls[0]["payload"] == target["payload"]


def test_backup_writer_uses_boundary_aware_replacement(tmp_path: Path) -> None:
    source = SCRIPT.read_text(encoding="utf-8")
    assert "replace_bytes_safely" in source
    assert ".write_text(" not in source


def test_backup_writer_rejects_hardlink_target(tmp_path: Path) -> None:
    backup_dir = tmp_path / "backup"
    backup_dir.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text("sentinel", encoding="utf-8")
    target = backup_dir / "payloads.jsonl"
    try:
        target.hardlink_to(outside)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"hardlinks unavailable: {exc}")

    with pytest.raises(OSError, match="hardlink"):
        module._write_backup(backup_dir, [_target()], {"expected_user_id": "user-1", "summary": {"target_digest": "digest"}})
    assert outside.read_text(encoding="utf-8") == "sentinel"


def test_backup_loader_rejects_reparse_backup_directory(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    backup_dir = tmp_path / "backup-link"
    try:
        backup_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(OSError, match="symlink/junction/reparse"):
        module._load_backup(backup_dir)
