from __future__ import annotations

from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException

from blackholememory import app as bhm_app


def test_admin_snapshot_loader_rejects_malformed_and_non_object_payload(tmp_path) -> None:
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{", encoding="utf-8")
    with pytest.raises(HTTPException) as malformed_error:
        bhm_app._load_admin_snapshot_payload(malformed)
    assert malformed_error.value.status_code == 422

    array_payload = tmp_path / "array.json"
    array_payload.write_text("[]", encoding="utf-8")
    with pytest.raises(HTTPException) as array_error:
        bhm_app._load_admin_snapshot_payload(array_payload)
    assert array_error.value.status_code == 422

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (bhm_app.MAX_ADMIN_SNAPSHOT_BYTES + 1))
    with pytest.raises(HTTPException) as oversized_error:
        bhm_app._load_admin_snapshot_payload(oversized)
    assert oversized_error.value.status_code == 413


def test_admin_snapshot_canonicalizer_rejects_unknown_artifact_type() -> None:
    with pytest.raises(HTTPException) as error:
        bhm_app._canonicalize_admin_snapshot_payload(
            {"project": "blackholememory", "memories": [], "links": [], "artifacts": {"unknown": []}},
            "blackholememory",
        )
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "admin_snapshot_unknown_artifact_type"


def test_admin_import_apply_rejects_unknown_merge_mode() -> None:
    request = bhm_app.AdminImportApplyRequest(path="snapshot.json", merge_mode="merge")
    with pytest.raises(HTTPException) as error:
        bhm_app._admin_import_apply(request, http_request=SimpleNamespace())
    assert error.value.status_code == 422
    assert error.value.detail["code"] == "admin_snapshot_merge_mode_invalid"


def test_admin_export_filters_alias_and_foreign_nested_records(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", tmp_path)
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            {"source_id": "local", "project": "BlackHoleMemory", "content": "local", "memory_type": "fact", "tags": [], "metadata": {}},
            {"source_id": "foreign", "project": "e-github-workspace", "content": "foreign", "memory_type": "fact", "tags": [], "metadata": {}},
        ],
    )
    monkeypatch.setattr(
        bhm_app,
        "_load_memory_links",
        lambda: [
            {"id": "local-link", "project": "blackholememory"},
            {"id": "foreign-link", "project": "e-github-workspace"},
        ],
    )
    monkeypatch.setattr(
        bhm_app,
        "_artifact_store_pairs",
        lambda: {"checkpoint": (lambda: [
            {"id": "local-artifact", "project": "BlackHoleMemory"},
            {"id": "foreign-artifact", "project": "e-github-workspace"},
        ], lambda _items: None)},
    )

    result = bhm_app._admin_export(
        bhm_app.AdminExportRequest(project="BlackHoleMemory", export_name="scoped.json")
    )
    payload = json.loads((tmp_path / "admin-exports" / "scoped.json").read_text(encoding="utf-8"))

    assert result["memory_count"] == 1
    assert payload["project"] == "blackholememory"
    assert {item["id"] for item in payload["links"]} == {"local-link"}
    assert {item["id"] for item in payload["artifacts"]["checkpoint"]} == {"local-artifact"}


def test_admin_export_rejects_linked_export_parent_before_creation(monkeypatch, tmp_path) -> None:
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    export_root = runtime_root / "admin-exports"
    try:
        export_root.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks unavailable on this Windows host")
    monkeypatch.setattr(bhm_app.settings, "runtime_dir", runtime_root)

    with pytest.raises(OSError, match="symlink|junction|reparse"):
        bhm_app._admin_export(bhm_app.AdminExportRequest(export_name="blocked.json"))
    assert not (outside / "blocked.json").exists()


def test_admin_snapshot_rejects_cross_project_link_endpoints(monkeypatch) -> None:
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            {"source_id": "local", "project": "blackholememory"},
            {"source_id": "foreign", "project": "e-github-workspace"},
        ],
    )
    monkeypatch.setattr(bhm_app, "_artifact_store_pairs", lambda: {})

    with pytest.raises(HTTPException) as error:
        bhm_app._validate_admin_snapshot_ownership(
            {
                "memories": [{"source_id": "local", "project": "blackholememory"}],
                "links": [
                    {
                        "source_id": "local",
                        "target_id": "foreign",
                        "relation": "relates_to",
                        "project": "blackholememory",
                    }
                ],
                "artifacts": {},
            }
        )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "caller_project_forbidden"


def test_admin_snapshot_rejects_artifact_backing_memory_from_foreign_project(monkeypatch) -> None:
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [
            {"source_id": "local", "project": "blackholememory"},
            {"source_id": "foreign", "project": "e-github-workspace"},
        ],
    )
    monkeypatch.setattr(
        bhm_app,
        "_artifact_store_pairs",
        lambda: {"checkpoint": (lambda: [], lambda _items: None)},
    )

    with pytest.raises(HTTPException) as error:
        bhm_app._validate_admin_snapshot_ownership(
            {
                "memories": [{"source_id": "local", "project": "blackholememory"}],
                "links": [],
                "artifacts": {
                    "checkpoint": [
                        {
                            "id": "checkpoint-1",
                            "project": "blackholememory",
                            "memory_id": "foreign",
                        }
                    ]
                },
            }
        )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "caller_project_forbidden"


def test_admin_snapshot_rejects_foreign_memory_id_collision_before_merge(monkeypatch) -> None:
    monkeypatch.setattr(
        bhm_app,
        "_load_live_memories",
        lambda: [{"source_id": "shared-id", "project": "e-github-workspace"}],
    )
    monkeypatch.setattr(bhm_app, "_artifact_store_pairs", lambda: {})

    with pytest.raises(HTTPException) as error:
        bhm_app._validate_admin_snapshot_ownership(
            {
                "memories": [{"source_id": "shared-id", "project": "blackholememory"}],
                "links": [],
                "artifacts": {},
            }
        )

    assert error.value.status_code == 403
    assert error.value.detail["code"] == "caller_project_forbidden"
