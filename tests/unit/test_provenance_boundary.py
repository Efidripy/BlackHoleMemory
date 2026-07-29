from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

from blackholememory.provenance_boundary import canonical_provenance_digest, scan_package_boundary


def test_provenance_digest_is_order_independent_and_identity_bound() -> None:
    rows = [
        {"source_id": "B", "slug": "b", "revision": "2", "content_sha256": "b" * 64, "manifest_sha256": "2" * 64},
        {"source_id": "A", "slug": "a", "revision": "1", "content_sha256": "a" * 64, "manifest_sha256": "1" * 64},
    ]
    assert canonical_provenance_digest(rows) == canonical_provenance_digest(list(reversed(rows)))
    changed = [dict(rows[0], revision="changed"), rows[1]]
    assert canonical_provenance_digest(rows) != canonical_provenance_digest(changed)


def test_package_boundary_rejects_src_in_directory_and_zip(tmp_path: Path) -> None:
    clean = tmp_path / "clean"
    clean.mkdir()
    (clean / "app.txt").write_text("ok", encoding="utf-8")
    assert scan_package_boundary(clean)["ok"] is True

    dirty = tmp_path / "dirty" / ".src" / "foreign"
    dirty.mkdir(parents=True)
    (dirty / "SOURCE-MANIFEST.json").write_text("{}", encoding="utf-8")
    dirty_report = scan_package_boundary(tmp_path / "dirty")
    assert dirty_report["ok"] is False
    assert any(".src" in item for item in dirty_report["residue"])

    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("BlackHoleMemory/app.txt", "ok")
        handle.writestr("BlackHoleMemory/.src/foreign/SOURCE-MANIFEST.json", "{}")
    archive_report = scan_package_boundary(archive)
    assert archive_report["ok"] is False


def test_validator_script_is_importable() -> None:
    script = Path(__file__).resolve().parents[2] / "scripts" / "validate-bhm-p28-provenance-boundary.py"
    spec = importlib.util.spec_from_file_location("validate_provenance_boundary", script)
    assert spec is not None and spec.loader is not None
