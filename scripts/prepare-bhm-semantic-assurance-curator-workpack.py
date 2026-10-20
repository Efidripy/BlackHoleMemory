#!/usr/bin/env python
"""Create an isolated, source-bound V6 curator workpack without evidence rows.

The workpack is deliberately only an authoring aid.  It copies the current
allowed Markdown sources, the exact read-only verifier, empty working ledgers,
and a digest manifest.  It never chooses fragments, derives cases, labels
content, calls a model, or writes BHM storage.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

GIT_TIMEOUT_SECONDS = 30


class WorkpackError(ValueError):
    """The requested workpack would not bind a safe canonical source snapshot."""


_ATOMIC_SKELETON = {
    "schema_version": "bhm.semantic-assurance-human-curation-ledger.v1",
    "curator_identity_attested": False,
    "atomic_facts": [],
}
_CONTROL_SKELETON = {
    "schema_version": "bhm.semantic-assurance-human-curation-controls.v1",
    "control_cells": [],
}


def build_workpack(canonical_root: Path, output: Path) -> dict[str, Any]:
    """Materialize one new non-overwriting workpack from a clean Git checkout."""

    root = canonical_root.resolve(strict=True)
    if not root.is_dir() or root.is_symlink():
        raise WorkpackError("canonical root is invalid")
    if output.exists():
        raise WorkpackError("output already exists")
    git_head = _clean_git_head(root)
    sources = _source_files(root)
    if not sources:
        raise WorkpackError("canonical root has no allowed Markdown sources")

    output.mkdir(parents=True)
    try:
        copied: list[dict[str, str]] = []
        for source in sources:
            relative = source.relative_to(root)
            target = output / "canonical-source" / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            copied.append({"path": relative.as_posix(), "sha256": _sha256(source)})

        validator_package = output / "validator" / "blackholememory"
        validator_package.mkdir(parents=True)
        (validator_package / "__init__.py").write_text("\"\"\"Portable curator validator package.\"\"\"\n", encoding="utf-8")
        shutil.copyfile(
            root / "src" / "blackholememory" / "semantic_assurance_curator_audit.py",
            validator_package / "semantic_assurance_curator_audit.py",
        )
        evidence_root = output / "owner-only" / "curator-source-evidence"
        evidence_root.mkdir(parents=True)
        _write_json(evidence_root / "atomic-facts.working.v2.json", _ATOMIC_SKELETON)
        _write_json(evidence_root / "control-cells.working.v2.json", _CONTROL_SKELETON)
        (output / "portable_validate_curator.py").write_text(_PORTABLE_VALIDATOR, encoding="utf-8")
        (output / "RUN-ROW-CHECKPOINT.cmd").write_text(_ROW_CHECKPOINT_CMD, encoding="utf-8", newline="\r\n")
        (output / "RUN-FULL-AUDIT.cmd").write_text(_FULL_AUDIT_CMD, encoding="utf-8", newline="\r\n")
        (output / "README-RU.md").write_text(_README, encoding="utf-8")
        (output / "ROW-SCHEMA-RU.md").write_text(_ROW_SCHEMA, encoding="utf-8")
        (output / "SEALED-LABEL-QUOTA-PLAN-RU.md").write_text(_QUOTA_PLAN, encoding="utf-8")
        manifest = {
            "schema_version": "bhm.semantic-assurance-curator-workpack.v1",
            "git_head": git_head,
            "canonical_source_file_count": len(copied),
            "canonical_source_snapshot_digest": _digest(copied),
            "validator_sha256": _sha256(root / "src" / "blackholememory" / "semantic_assurance_curator_audit.py"),
            "execution": {
                "network_used": False,
                "model_called": False,
                "sqlite_mutation": False,
                "mem0_mutation": False,
                "qdrant_mutation": False,
                "automatic_apply": False,
            },
        }
        _write_json(output / "SOURCE-SNAPSHOT-MANIFEST.json", manifest)
        return manifest
    except Exception:
        shutil.rmtree(output)
        raise


def _clean_git_head(root: Path) -> str:
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            check=True,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        ).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise WorkpackError("canonical root must be a Git checkout") from exc
    if len(head) != 40 or any(char not in "0123456789abcdef" for char in head) or status:
        raise WorkpackError("canonical Git checkout must be clean")
    return head


def _source_files(root: Path) -> list[Path]:
    files = [* (root / "docs").rglob("*.md")]
    local_adr = root / ".docs" / "adr"
    if local_adr.exists():
        files.extend(local_adr.rglob("*.md"))
    result: list[Path] = []
    for path in sorted(files):
        if not path.is_file() or path.is_symlink():
            raise WorkpackError("canonical source is not a regular Markdown file")
        path.resolve(strict=True).relative_to(root)
        result.append(path)
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = build_workpack(args.canonical_root, args.output)
    except (OSError, WorkpackError, ValueError) as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps({"ok": True, **result}, ensure_ascii=False, sort_keys=True))
    return 0


_PORTABLE_VALIDATOR = r'''#!/usr/bin/env python
"""Run the copied curator verifier without generating evidence."""
from __future__ import annotations
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "validator"))
from blackholememory.semantic_assurance_curator_audit import (  # noqa: E402
    SemanticAssuranceCuratorAuditError,
    audit_v6_curator_inputs,
)

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--atomic-facts", type=Path, required=True)
    parser.add_argument("--control-cells", type=Path, required=True)
    parser.add_argument("--canonical-root", type=Path, required=True)
    parser.add_argument("--row-checkpoint", action="store_true")
    args = parser.parse_args()
    try:
        facts = json.loads(args.atomic_facts.read_text(encoding="utf-8"))
        controls = json.loads(args.control_cells.read_text(encoding="utf-8"))
        receipt = audit_v6_curator_inputs(facts, controls, canonical_root=args.canonical_root)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, SemanticAssuranceCuratorAuditError, ValueError) as exc:
        if args.row_checkpoint and str(exc) in {
            "atomic fact partition coverage is below V6 minimum",
            "curation controls lack one or more risks in a partition",
            "sealed curator conflict coverage is below V6 minimum",
        }:
            print(json.dumps({"ok": True, "stage": "row-binding-checkpoint", "coverage_complete": False, "automatic_apply_ready": False, "decision": "no_go"}, ensure_ascii=False))
            return 0
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)}, ensure_ascii=False))
        return 2
    print(json.dumps(receipt, ensure_ascii=False, sort_keys=True))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
'''

_ROW_CHECKPOINT_CMD = """@echo off
setlocal
python portable_validate_curator.py --atomic-facts owner-only\\curator-source-evidence\\atomic-facts.working.v2.json --control-cells owner-only\\curator-source-evidence\\control-cells.working.v2.json --canonical-root canonical-source --row-checkpoint
"""

_FULL_AUDIT_CMD = """@echo off
setlocal
python portable_validate_curator.py --atomic-facts owner-only\\curator-source-evidence\\atomic-facts.working.v2.json --control-cells owner-only\\curator-source-evidence\\control-cells.working.v2.json --canonical-root canonical-source
"""

_README = """# V6 curator recovery workpack

Это self-contained набор для **ручной** рекурации. Он не содержит готовых
facts, cases, labels, reviewer returns или admission receipts. Старые rows не
копируйте и не переписывайте под новые floors.

1. Работайте только с `canonical-source/`. В `atomic-facts.working.v2.json`
   вручную внесите три реальные, проверенные человеком source bindings: по
   одному для `development`, `validation`, `sealed_holdout`. Не выбирайте
   источники через LLM.
2. Для каждого такого fact добавьте ровно одну paired row в
   `control-cells.working.v2.json`. Поля и закрытые vocabularies приведены в
   `ROW-SCHEMA-RU.md`. Перед массовой курацией прочтите
   `SEALED-LABEL-QUOTA-PLAN-RU.md`: это sampling plan, а не labels.
3. Запустите `RUN-ROW-CHECKPOINT.cmd`. Успех означает только, что имеющиеся
   rows корректно bind к snapshot; `coverage_complete=false` ожидаем, пока не
   будет полный набор.
4. Доведите набор до реальных `220 development + 220 validation + 400
   sealed_holdout`, с 11 risk classes в каждой partition и не менее 50
   sealed rows для каждого conflict risk. Затем `RUN-FULL-AUDIT.cmd` должен
   выдать content-free receipt.
5. Перед тем как этот receipt будет использован дальше, владелец снова
   запускает source audit против чистого canonical repository на exact
   `git_head` из `SOURCE-SNAPSHOT-MANIFEST.json`. Snapshot удобен для работы,
   но не заменяет final current-source revalidation.

Никакой успешный чекпоинт или полный audit не доказывает factual truth,
entailment, relevance, human identity/independence, hidden-conflict recall
или готовность automatic apply. `automatic_apply_ready=false`, `no_go`.
"""

_QUOTA_PLAN = """# Sealed label quota plan — не labels

Этот план предотвращает повторение structural-only corpus без отрицательных и
conflict observations. Он задаёт **только sampling intent** до курации. Не
вписывайте здесь или в curator ledgers expected/final labels: их независимо
определяют два blinded reviewer-а и adjudicator после построения corpus.

| Final-adjudication stratum | Sealed minimum | Минимальный безопасный sampling intent |
| --- | ---: | --- |
| `factuality=false`, `ambiguous`, `insufficient` | 80 each | Три отдельные группы source situations; каждая spread хотя бы по двум risk classes. |
| `entailment=contradicted`, `insufficient` | 80 each | Отдельные support/candidate relations; каждая spread хотя бы по двум risks. |
| `support_relevance=irrelevant` | 80 support rows | Реальные candidate/support combinations с проверяемой нерелевантностью; spread по двум risks. |
| `conflict=direct`, `temporal`, `scope`, `implicit` | 80 each | По `>=50` cases в aligned risk (`*_conflict`) и `>=30` в другом risk class. |

Четыре conflict labels mutually exclusive at case level, поэтому их minima уже
занимают `320` из `400` sealed cases. Оставшиеся `80` нельзя считать запасом
для подгонки: они нужны для coverage остальных 7 risk classes и естественного
исхода независимой adjudication. В каждой partition должны присутствовать все
11 risks; exact label strata могут быть засчитаны только final adjudication.

Если final V5/V6 admission показывает deficit, старые sealed rows/labels не
переписывают. Создают новые independently authored cases, начиная с новых
human-verified source facts и с сохранением split isolation. Этот документ не
является admission, model prompt или разрешением automatic apply.
"""

_ROW_SCHEMA = """# Схема ручных rows

## atomic facts

Верхний объект: `schema_version`, `curator_identity_attested`, `atomic_facts`.
Когда началась реальная курация, `curator_identity_attested` ставит `true`
человек-куратор. Каждый fact имеет **ровно** эти поля:

`atomic_fact_id`, `partition`, `canonical_path`, `canonical_heading`,
`line_start`, `line_end`, `fragment_sha256`, `proposition_private`,
`inference_mechanism_private`, `relation_form_private`,
`source_verified_by_human`.

- `atomic_fact_id` — opaque SHA-256, не идентификатор риска или partition.
- `partition` — `development`, `validation` или `sealed_holdout`.
- `canonical_path` — relative POSIX path под `docs/` или `.docs/adr/`.
- `canonical_heading` — точный Markdown heading, owning указанный range.
- `line_start`/`line_end` — inclusive one-based lines; `fragment_sha256` —
  SHA-256 LF-normalized selected lines без terminal newline.
- `proposition_private` и `inference_mechanism_private` уникальны между всеми
  rows и partitions; они остаются private.
- `relation_form_private` — `support`, `contradiction`, `absence`, `temporal`,
  `scope` или `relevance`; `source_verified_by_human=true`.

## control cells

Верхний объект: `schema_version`, `control_cells`. Каждая cell имеет ровно:

`atomic_fact_id`, `risk_class`, `support_count_bucket`,
`approximate_length_bucket`, `document_genre`, `relation_form`,
`adversarial_form`, `dimension_surfaces`.

`risk_class`: `factuality`, `entailment`, `relevance`, `direct_conflict`,
`temporal_conflict`, `scope_conflict`, `implicit_conflict`, `injection`,
`cross_project`, `retrieval_pressure`, `abstention`.

`support_count_bucket`: `one`, `two`, `three_plus`; `approximate_length_bucket`:
`short`, `medium`, `long`; `document_genre`: `architecture`, `api_contract`,
`governance`, `runbook`, `source_code`, `test`; `relation_form` совпадает с
закрытым словарём facts; `adversarial_form`: `none`, `ambiguity`, `conflict`,
`cross_project`, `injection`, `retrieval_pressure`; `dimension_surfaces` —
непустой уникальный subset `factuality`, `entailment`, `relevance`,
`safe_operation`.

Одна cell соответствует ровно одному fact. Не создавайте synthetic rows, не
копируйте одну source fragment дважды, не создавайте reviewer-visible case и
не добавляйте labels на этой стадии.
"""


if __name__ == "__main__":
    raise SystemExit(main())
