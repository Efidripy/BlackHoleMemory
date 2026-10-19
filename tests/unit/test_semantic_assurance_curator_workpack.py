from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).parents[2]
SCRIPT = ROOT / "scripts" / "prepare-bhm-semantic-assurance-curator-workpack.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("bhm_curator_workpack", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True)


def test_workpack_snapshot_and_partial_row_checkpoint(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "docs").mkdir(parents=True)
    validator = source / "src" / "blackholememory"
    validator.mkdir(parents=True)
    markdown = "# Contract\n\nVerified fact.\n"
    (source / "docs" / "contract.md").write_text(markdown, encoding="utf-8")
    (validator / "__init__.py").write_text("", encoding="utf-8")
    shutil.copyfile(ROOT / "src" / "blackholememory" / "semantic_assurance_curator_audit.py", validator / "semantic_assurance_curator_audit.py")
    _git(source, "init")
    _git(source, "config", "user.email", "test@example.invalid")
    _git(source, "config", "user.name", "Test")
    _git(source, "add", "docs/contract.md", "src/blackholememory")
    _git(source, "commit", "-m", "fixture")

    workpack = tmp_path / "workpack"
    module = _load_module()
    result = module.build_workpack(source, workpack)
    assert result["canonical_source_file_count"] == 1
    assert (workpack / "canonical-source" / "docs" / "contract.md").read_text(encoding="utf-8") == markdown

    fact_id = _sha("fact")
    facts = {
        "schema_version": "bhm.semantic-assurance-human-curation-ledger.v1",
        "curator_identity_attested": True,
        "atomic_facts": [{
            "atomic_fact_id": fact_id,
            "partition": "development",
            "canonical_path": "docs/contract.md",
            "canonical_heading": "Contract",
            "line_start": 1,
            "line_end": 3,
            "fragment_sha256": _sha("# Contract\n\nVerified fact."),
            "proposition_private": "one verified proposition",
            "inference_mechanism_private": "one human verification mechanism",
            "relation_form_private": "support",
            "source_verified_by_human": True,
        }],
    }
    controls = {
        "schema_version": "bhm.semantic-assurance-human-curation-controls.v1",
        "control_cells": [{
            "atomic_fact_id": fact_id,
            "risk_class": "factuality",
            "support_count_bucket": "one",
            "approximate_length_bucket": "short",
            "document_genre": "governance",
            "relation_form": "support",
            "adversarial_form": "none",
            "dimension_surfaces": ["factuality"],
        }],
    }
    evidence = workpack / "owner-only" / "curator-source-evidence"
    (evidence / "atomic-facts.working.v2.json").write_text(json.dumps(facts), encoding="utf-8")
    (evidence / "control-cells.working.v2.json").write_text(json.dumps(controls), encoding="utf-8")
    completed = subprocess.run(
        [
            sys.executable,
            str(workpack / "portable_validate_curator.py"),
            "--atomic-facts", str(evidence / "atomic-facts.working.v2.json"),
            "--control-cells", str(evidence / "control-cells.working.v2.json"),
            "--canonical-root", str(workpack / "canonical-source"),
            "--row-checkpoint",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert json.loads(completed.stdout) == {
        "ok": True,
        "stage": "row-binding-checkpoint",
        "coverage_complete": False,
        "automatic_apply_ready": False,
        "decision": "no_go",
    }


def test_workpack_rejects_existing_output(tmp_path: Path) -> None:
    module = _load_module()
    output = tmp_path / "existing"
    output.mkdir()
    try:
        module.build_workpack(tmp_path, output)
    except module.WorkpackError as exc:
        assert str(exc) == "output already exists"
    else:
        raise AssertionError("existing workpack output must be rejected")
