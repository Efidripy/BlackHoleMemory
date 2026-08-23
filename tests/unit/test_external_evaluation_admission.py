from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from blackholememory.external_evaluation_admission import ExternalEvaluationAdmissionError
from blackholememory.external_evaluation_admission import SCHEMA_VERSION
from blackholememory.external_evaluation_admission import load_external_evaluation_admission_report
from blackholememory.external_evaluation_admission import validate_external_evaluation_dataset_admission
from blackholememory.external_evaluation_admission import verify_external_evaluation_admission_report


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _manifest(root, *, dataset_digest: str | None = None, review_status: str = "approved-local-evaluation-only"):
    dataset = b'{"synthetic":"local external evaluation slice"}\n'
    license_text = b"License evidence for disposable unit test\n"
    (root / "dataset.json").write_bytes(dataset)
    (root / "LICENSE.txt").write_bytes(license_text)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "dataset": {
            "suite": "locomo",
            "version": "fixture-v1",
            "path": "dataset.json",
            "sha256": dataset_digest or _digest(dataset),
            "source_url": "https://github.com/snap-research/locomo",
            "source_revision": "a" * 40,
            "license": {"spdx": "CC-BY-4.0", "evidence_path": "LICENSE.txt", "evidence_sha256": _digest(license_text)},
        },
        "review": {"status": review_status, "reviewer": "operator-fixture", "reviewed_at": "2026-08-23T00:00:00Z"},
    }
    path = root / "admission.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_external_dataset_admission_is_local_only_and_content_free(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    report = validate_external_evaluation_dataset_admission(tmp_path, manifest)

    assert report["ok"] is True
    assert report["dataset"]["suite"] == "locomo"
    assert report["execution"] == {
        "network": False,
        "dataset_content_emitted": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "runtime_feature_enabled": False,
    }
    assert "dataset.json" not in str(report)
    assert "snap-research" not in str(report)


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"dataset_digest": "0" * 64}, "dataset.sha256 does not match local dataset"),
        ({"review_status": "pending"}, "review status is not approved for local evaluation"),
    ],
)
def test_external_dataset_admission_fails_closed_for_digest_or_review(tmp_path, kwargs, error: str) -> None:
    manifest = _manifest(tmp_path, **kwargs)
    with pytest.raises(ExternalEvaluationAdmissionError, match=error):
        validate_external_evaluation_dataset_admission(tmp_path, manifest)


def test_external_dataset_admission_rejects_manifest_outside_root(tmp_path) -> None:
    outside = tmp_path.parent / "external-admission.json"
    outside.write_text("{}", encoding="utf-8")
    with pytest.raises(ExternalEvaluationAdmissionError, match="inside dataset root"):
        validate_external_evaluation_dataset_admission(tmp_path, outside)


def test_external_dataset_admission_cli_is_bounded_and_content_free(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    script = Path(__file__).resolve().parents[2] / "scripts" / "validate-bhm-external-evaluation-dataset.py"
    result = subprocess.run(
        [sys.executable, str(script), "--dataset-root", str(tmp_path), "--manifest", str(manifest)],
        capture_output=True,
        check=False,
        encoding="utf-8",
        text=True,
        timeout=10,
    )

    report = json.loads(result.stdout)
    assert result.returncode == 0
    assert report["ok"] is True
    assert report["execution"]["network"] is False
    assert "dataset.json" not in result.stdout


def test_admission_report_revalidation_rejects_tampered_digest_or_mutation_flags(tmp_path) -> None:
    manifest = _manifest(tmp_path)
    report = validate_external_evaluation_dataset_admission(tmp_path, manifest)
    report_path = tmp_path / "admission-report.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert load_external_evaluation_admission_report(report_path) == report
    assert verify_external_evaluation_admission_report(report) == report

    tampered_digest = {**report, "admission_digest": "0" * 64}
    with pytest.raises(ExternalEvaluationAdmissionError, match="digest mismatch"):
        verify_external_evaluation_admission_report(tampered_digest)

    tampered_execution = {**report, "execution": {**report["execution"], "network": True}}
    with pytest.raises(ExternalEvaluationAdmissionError, match="execution boundary"):
        verify_external_evaluation_admission_report(tampered_execution)
