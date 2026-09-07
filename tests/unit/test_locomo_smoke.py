from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from blackholememory.locomo_smoke import ADVERSARIAL_ROUTE
from blackholememory.locomo_smoke import LoCoMoSmokeError
from blackholememory.locomo_smoke import ROUTE
from blackholememory.locomo_smoke import run_locomo_lexical_smoke


_SECRET = "locomo-private-fixture-content"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _admission(dataset_digest: str, *, version: str = "fixture-v1") -> dict[str, object]:
    core: dict[str, object] = {
        "schema_version": "bhm.evaluation.external-dataset-admission.v1",
        "ok": True,
        "dataset": {
            "suite": "locomo",
            "version": version,
            "dataset_digest": dataset_digest,
            "source_url_digest": "b" * 64,
            "source_revision": "a" * 40,
            "license_spdx": "CC-BY-NC-4.0",
            "license_evidence_digest": "c" * 64,
        },
        "review": {
            "status": "approved-local-evaluation-only",
            "reviewer_digest": "d" * 64,
            "reviewed_at": "2026-09-07T00:00:00Z",
        },
        "execution": {
            "network": False,
            "dataset_content_emitted": False,
            "model_calls": 0,
            "sqlite_mutation": False,
            "qdrant_mutation": False,
            "mem0_mutation": False,
            "runtime_feature_enabled": False,
        },
    }
    digest = _sha256(json.dumps(core, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    return {**core, "admission_digest": digest}


def _dataset() -> list[dict[str, object]]:
    samples: list[dict[str, object]] = []
    for category in range(1, 6):
        qa: dict[str, object] = {
            "category": category,
            "question": f"needle-{category}",
            "evidence": ["(D1:1)"],
        }
        if category == 5:
            qa["adversarial_answer"] = "incorrect fixture answer"
        else:
            qa["answer"] = "fixture answer"
        samples.append(
            {
                "sample_id": f"sample-{category}",
                "conversation": {
                    "speaker_a": "A",
                    "session_1": [
                        {
                            "dia_id": "D1:1",
                            "speaker": "A",
                            "text": f"{_SECRET} needle-{category}",
                            "img_url": ["https://example.invalid/image.png"],
                        }
                    ],
                },
                "qa": [qa],
                "observation": {},
                "session_summary": {},
                "event_summary": {},
            }
        )
    return samples


def _write_admitted_dataset(tmp_path: Path, payload: list[dict[str, object]] | None = None) -> tuple[Path, Path, Path]:
    root = tmp_path / "locomo-root"
    root.mkdir(parents=True)
    dataset = root / "locomo10.json"
    dataset_bytes = json.dumps(payload if payload is not None else _dataset(), ensure_ascii=False).encode("utf-8")
    dataset.write_bytes(dataset_bytes)
    admission = root / "admission-report.json"
    admission.write_text(json.dumps(_admission(_sha256(dataset_bytes))), encoding="utf-8")
    return root, dataset, admission


def _run(root: Path, dataset: Path, admission: Path, *, max_cases: int = 5) -> dict[str, object]:
    return run_locomo_lexical_smoke(
        root,
        dataset,
        dataset_version="fixture-v1",
        admission_report=json.loads(admission.read_text(encoding="utf-8")),
        max_cases=max_cases,
        k=3,
    )


def test_locomo_adapter_is_deterministic_content_free_and_has_explicit_category_mapping(tmp_path: Path) -> None:
    root, dataset, admission = _write_admitted_dataset(tmp_path)
    first = _run(root, dataset, admission)
    second = _run(root, dataset, admission)

    assert first["manifest"] == second["manifest"]
    manifest = first["manifest"]
    assert isinstance(manifest, dict)
    assert [case["category"] for case in manifest["cases"]] == [
        "multi_hop", "temporal", "open_domain", "single_hop", "adversarial"
    ]
    receipts = first["receipts"]
    assert isinstance(receipts, list)
    assert receipts[-1]["route"] == ADVERSARIAL_ROUTE
    assert receipts[-1]["retrieved_ids"] == []
    assert receipts[-1]["abstained"] is True
    assert all(receipt["route"] == ROUTE for receipt in receipts[:-1])
    report = first["report"]
    assert isinstance(report, dict)
    assert report["execution"] == {
        "network": False,
        "dataset_content_emitted": False,
        "image_url_retrieval": False,
        "model_calls": 0,
        "sqlite_mutation": False,
        "qdrant_mutation": False,
        "mem0_mutation": False,
        "runtime_feature_enabled": False,
        "source_evidence_exclusion_count": 0,
        "source_missing_retrieval_evidence_exclusion_count": 0,
        "source_unresolvable_evidence_exclusion_count": 0,
        "route": ROUTE,
        "adversarial_route": ADVERSARIAL_ROUTE,
    }
    serialized = json.dumps(first, ensure_ascii=False)
    assert _SECRET not in serialized
    assert "fixture answer" not in serialized
    assert "example.invalid" not in serialized


def test_locomo_adapter_rejects_unknown_category_and_bad_selected_evidence(tmp_path: Path) -> None:
    unknown = _dataset()
    unknown[0]["qa"][0]["category"] = 6  # type: ignore[index]
    root, dataset, admission = _write_admitted_dataset(tmp_path / "unknown", unknown)
    with pytest.raises(LoCoMoSmokeError, match="unsupported"):
        _run(root, dataset, admission)

    invalid = _dataset()
    invalid[0]["qa"][0]["evidence"] = ["D9:9"]  # type: ignore[index]
    root, dataset, admission = _write_admitted_dataset(tmp_path / "invalid", invalid)
    with pytest.raises(LoCoMoSmokeError, match="admissible"):
        _run(root, dataset, admission)

    missing_evidence = _dataset()
    missing_evidence[2]["qa"][0]["evidence"] = []  # type: ignore[index]
    root, dataset, admission = _write_admitted_dataset(tmp_path / "missing-evidence", missing_evidence)
    with pytest.raises(LoCoMoSmokeError, match="admissible"):
        _run(root, dataset, admission)


def test_locomo_selection_interleaves_samples_within_each_category(tmp_path: Path) -> None:
    payload = _dataset()
    copied: list[dict[str, object]] = []
    for index, original in enumerate(payload, start=1):
        clone = json.loads(json.dumps(original))
        clone["sample_id"] = f"other-{index}"
        clone["qa"][0]["question"] = f"other-needle-{index}"
        clone["conversation"]["session_1"][0]["text"] = f"{_SECRET} other-needle-{index}"
        copied.append(clone)
    root, dataset, admission = _write_admitted_dataset(tmp_path, payload + copied)
    result = _run(root, dataset, admission, max_cases=10)

    manifest = result["manifest"]
    assert isinstance(manifest, dict)
    cases = manifest["cases"]
    assert {case["session_id"] for case in cases} == {
        "sample-1", "sample-2", "sample-3", "sample-4", "sample-5",
        "other-1", "other-2", "other-3", "other-4", "other-5",
    }

def test_locomo_adapter_rejects_nonmatching_admission_and_dataset_outside_root(tmp_path: Path) -> None:
    root, dataset, admission = _write_admitted_dataset(tmp_path)
    report = json.loads(admission.read_text(encoding="utf-8"))
    report["dataset"]["dataset_digest"] = "0" * 64
    with pytest.raises(LoCoMoSmokeError, match="admission report is invalid"):
        run_locomo_lexical_smoke(root, dataset, dataset_version="fixture-v1", admission_report=report, max_cases=5)

    outside = tmp_path / "outside.json"
    outside.write_bytes(dataset.read_bytes())
    with pytest.raises(LoCoMoSmokeError, match="inside dataset root"):
        _run(root, outside, admission)


def test_locomo_cli_writes_content_free_receipts_and_rejects_outside_admission(tmp_path: Path) -> None:
    root, dataset, admission = _write_admitted_dataset(tmp_path)
    output = tmp_path / "output"
    script = Path(__file__).resolve().parents[2] / "scripts" / "run-bhm-locomo-smoke.py"
    result = subprocess.run(
        [
            sys.executable,
            str(script),
            "--dataset-root", str(root),
            "--dataset", str(dataset),
            "--dataset-version", "fixture-v1",
            "--admission-report", str(admission),
            "--output-dir", str(output),
            "--max-cases", "5",
            "--k", "3",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["ok"] is True
    contents = "".join(path.read_text(encoding="utf-8") for path in sorted(output.glob("*.json")))
    assert _SECRET not in contents
    assert "fixture answer" not in contents
    assert "example.invalid" not in contents

    outside_admission = tmp_path / "outside-admission.json"
    outside_admission.write_bytes(admission.read_bytes())
    rejected = subprocess.run(
        [
            sys.executable,
            str(script),
            "--dataset-root", str(root),
            "--dataset", str(dataset),
            "--dataset-version", "fixture-v1",
            "--admission-report", str(outside_admission),
            "--output-dir", str(output),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert rejected.returncode == 2
    assert "inside dataset root" in json.loads(rejected.stdout)["error"]
