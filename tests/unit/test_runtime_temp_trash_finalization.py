from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path


SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "bhm-finalize-runtime-temp-trash.py"
SPEC = importlib.util.spec_from_file_location("runtime_temp_trash_finalization", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _stage(tmp_path: Path) -> tuple[Path, str]:
    runtime = tmp_path / ".runtime"
    target = runtime / "TEMP_TRASH" / "historical-prune-20260824T003612Z"
    target.mkdir(parents=True)
    external = tmp_path / "external" / "manifest.json"
    external.parent.mkdir()
    external.write_text(
        json.dumps(
            {
                "schemaVersion": "bhm.external-live-backup.v1",
                "sqlite_online_backups": [
                    {"source": name, "quick_check": "ok", "foreign_key_errors": 0}
                    for name in sorted(MODULE.EXPECTED_EXTERNAL_DATABASES)
                ],
            }
        ),
        encoding="utf-8",
    )
    (target / "payload.bin").write_bytes(b"historical")
    (target / "manifest.json").write_text(
        json.dumps(
            {
                "schemaVersion": "bhm.runtime-temp-trash.v1",
                "total_bytes": 10,
                "external_live_backup": str(external),
            }
        ),
        encoding="utf-8",
    )
    return runtime, target.name


def test_finalization_requires_exact_current_plan_digest(tmp_path: Path) -> None:
    runtime, stage = _stage(tmp_path)
    plan = MODULE.build_plan(runtime, stage)
    digest = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    result = MODULE.finalize(runtime, stage, digest)

    assert result["deleted"] is True
    assert not (runtime / "TEMP_TRASH" / stage).exists()


def test_finalization_rejects_wrong_digest_without_deleting(tmp_path: Path) -> None:
    runtime, stage = _stage(tmp_path)

    try:
        MODULE.finalize(runtime, stage, "0" * 64)
    except MODULE.TempTrashFinalizationError as exc:
        assert "plan digest mismatch" in str(exc)
    else:
        raise AssertionError("wrong digest was accepted")
    assert (runtime / "TEMP_TRASH" / stage).is_dir()


def test_finalization_removes_operator_owned_readonly_file(tmp_path: Path) -> None:
    runtime, stage = _stage(tmp_path)
    read_only = runtime / "TEMP_TRASH" / stage / "readonly.bin"
    read_only.write_bytes(b"old")
    read_only.chmod(0o444)
    plan = MODULE.build_plan(runtime, stage)
    digest = hashlib.sha256(
        json.dumps(plan, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()

    MODULE.finalize(runtime, stage, digest)

    assert not (runtime / "TEMP_TRASH" / stage).exists()
