"""Reviewed-only local-LLM learning loop for P17.20.

This module turns explicit human/operator reviews into bounded dataset
proposals. Accepted reviews become eval and few-shot examples; rejected
reviews become regression cases. Raw values are never persisted: only bounded,
sanitized examples, digests, validators and provenance are stored. No model
training, LoRA/QLoRA job or automatic application is started here.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .llm_safety import LLMSafetyViolation
from .llm_safety import sanitize_llm_value
from .llm_safety import scan_prompt_injection


LLM_LEARNING_SCHEMA_VERSION = 1
LLM_LEARNING_POLICY_VERSION = "bhm.llm.learning.v1"
LLM_LEARNING_MAX_RECORDS = 1_024
LLM_LEARNING_MAX_DATASET_RECORDS = 256
LLM_LEARNING_MAX_TEXT_BYTES = 64 * 1024
LLM_LEARNING_MAX_METADATA_BYTES = 32 * 1024
LLM_LEARNING_WRITE_RETRY_DELAYS = (0.025, 0.05, 0.1, 0.2, 0.4)


class LLMLearningError(ValueError):
    """Base error for reviewed-learning validation and storage."""


class LLMLearningBoundsError(LLMLearningError):
    """A reviewed-learning payload exceeded an explicit bound."""


class LLMLearningPrivacyError(LLMLearningError):
    """A payload cannot cross the learning privacy boundary."""


class LLMLearningCollision(LLMLearningError):
    """A deterministic review identity already names different evidence."""

    def __init__(self, source_job_id: str) -> None:
        self.source_job_id = str(source_job_id)
        super().__init__(f"llm learning review collision: {self.source_job_id}")


class LLMLearningStoreFull(LLMLearningError):
    """The bounded reviewed-learning store refuses unbounded growth."""


class LLMLearningReviewError(LLMLearningError):
    """A review decision is missing required human/evidence gates."""


class LLMLearningStore:
    """Bounded SQLite WAL store for explicit reviewed LLM outcomes."""

    def __init__(
        self,
        path: Path | str,
        *,
        max_records: int = LLM_LEARNING_MAX_RECORDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.path = Path(path)
        self.max_records = max(1, int(max_records))
        self._clock = clock
        self._initialize_lock = threading.Lock()
        self._initialized = False

    def initialize(self) -> None:
        if self._initialized:
            return
        with self._initialize_lock:
            if self._initialized:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)

            def create_schema() -> None:
                with closing(self._connect()) as connection:
                    current_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if current_version not in {0, LLM_LEARNING_SCHEMA_VERSION}:
                        raise LLMLearningError(
                            f"unsupported learning schema {current_version}; expected {LLM_LEARNING_SCHEMA_VERSION}"
                        )
                    journal_mode = str(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]).casefold()
                    if journal_mode != "wal":
                        raise LLMLearningError(f"SQLite refused WAL mode for {self.path}: {journal_mode}")
                    connection.executescript(
                        """
                        CREATE TABLE IF NOT EXISTS llm_learning_reviews (
                            record_id TEXT PRIMARY KEY,
                            project TEXT NOT NULL,
                            source_job_id TEXT NOT NULL,
                            decision TEXT NOT NULL,
                            dataset_kind TEXT NOT NULL,
                            reviewer TEXT NOT NULL,
                            review_reason TEXT NOT NULL,
                            prompt_version TEXT NOT NULL,
                            model_digest TEXT NOT NULL,
                            input_digest TEXT NOT NULL,
                            prompt_digest TEXT NOT NULL,
                            output_digest TEXT NOT NULL,
                            parameters_digest TEXT NOT NULL,
                            input_json TEXT NOT NULL,
                            prompt_json TEXT NOT NULL,
                            output_json TEXT NOT NULL,
                            parameters_json TEXT NOT NULL,
                            validation_json TEXT NOT NULL,
                            provenance_json TEXT NOT NULL,
                            safety_json TEXT NOT NULL,
                            record_digest TEXT NOT NULL,
                            reviewed_at TEXT NOT NULL
                        );
                        CREATE UNIQUE INDEX IF NOT EXISTS idx_llm_learning_source_job
                            ON llm_learning_reviews(project, source_job_id);
                        CREATE INDEX IF NOT EXISTS idx_llm_learning_decision
                            ON llm_learning_reviews(project, decision, reviewed_at DESC);
                        """
                    )
                    connection.execute(f"PRAGMA user_version={LLM_LEARNING_SCHEMA_VERSION}")
                    connection.commit()

            self._with_write_retry(create_schema, priority=True)
            self._initialized = True

    def record_review(
        self,
        *,
        project: str,
        source_job_id: str,
        decision: str,
        reviewer: str,
        review_reason: str,
        input_value: Any,
        prompt: str,
        output: Any,
        prompt_version: str = "default-v1",
        model_digest: str = "local-model",
        parameters: Mapping[str, Any] | None = None,
        validation: Mapping[str, Any] | None = None,
        provenance: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized_project = _normalize_token(project, "project", 120).casefold()
        normalized_job = _normalize_token(source_job_id, "source_job_id", 200)
        normalized_decision = _normalize_decision(decision)
        normalized_reviewer = _normalize_token(reviewer, "reviewer", 160)
        normalized_reason = _normalize_token(review_reason, "review_reason", 2_000)
        normalized_prompt_version = _normalize_token(prompt_version, "prompt_version", 120)
        normalized_model_digest = _normalize_token(model_digest, "model_digest", 160)

        safe_input, input_meta = _sanitize(
            input_value,
            source="llm-learning-input",
            project=normalized_project,
            max_bytes=LLM_LEARNING_MAX_TEXT_BYTES,
        )
        safe_prompt, prompt_meta = _sanitize(
            prompt,
            source="llm-learning-prompt",
            project=normalized_project,
            max_bytes=LLM_LEARNING_MAX_TEXT_BYTES,
        )
        safe_output, output_meta = _sanitize(
            output,
            source="llm-learning-output",
            project=normalized_project,
            max_bytes=LLM_LEARNING_MAX_TEXT_BYTES,
        )
        safe_parameters, _parameters_meta = _sanitize(
            dict(parameters or {}),
            source="llm-learning-parameters",
            project=normalized_project,
            max_bytes=LLM_LEARNING_MAX_METADATA_BYTES,
        )
        safe_validation, _validation_meta = _sanitize(
            dict(validation or {}),
            source="llm-learning-validation",
            project=normalized_project,
            max_bytes=LLM_LEARNING_MAX_METADATA_BYTES,
        )
        safe_provenance, _provenance_meta = _sanitize(
            dict(provenance or {}),
            source="llm-learning-provenance",
            project=normalized_project,
            max_bytes=LLM_LEARNING_MAX_METADATA_BYTES,
        )
        if not isinstance(safe_validation, dict):
            raise LLMLearningReviewError("validation must sanitize to an object")
        if not isinstance(safe_provenance, dict):
            raise LLMLearningReviewError("provenance must sanitize to an object")

        validation_passed, validator_count = _validation_gate(safe_validation)
        injection_findings = sorted(
            set(
                scan_prompt_injection(_flatten_text(safe_input))
                + scan_prompt_injection(_flatten_text(safe_prompt))
                + scan_prompt_injection(_flatten_text(safe_output))
            )
        )
        if normalized_decision == "accepted" and not validation_passed:
            raise LLMLearningReviewError("accepted review requires passed validators")
        if normalized_decision == "accepted" and injection_findings:
            raise LLMLearningPrivacyError("accepted review contains prompt-injection findings")

        dataset_kind = "eval_and_few_shot" if normalized_decision == "accepted" else "regression"
        input_json = _canonical_json(safe_input)
        prompt_json = _canonical_json(safe_prompt)
        output_json = _canonical_json(safe_output)
        parameters_json = _canonical_json(safe_parameters)
        validation_json = _canonical_json(safe_validation)
        provenance_json = _canonical_json(safe_provenance)
        input_digest = _sha256(input_json)
        prompt_digest = _sha256(prompt_json)
        output_digest = _sha256(output_json)
        parameters_digest = _sha256(parameters_json)
        safety_json = _canonical_json(
            {
                "policy_version": "bhm.llm.safety.v1",
                "injection_findings": injection_findings,
                "redaction_kinds": sorted(
                    set(input_meta.get("redaction_kinds", []))
                    | set(prompt_meta.get("redaction_kinds", []))
                    | set(output_meta.get("redaction_kinds", []))
                ),
                "raw_values_stored": False,
                "sanitized_examples_stored": True,
            }
        )
        core = {
            "schema_version": LLM_LEARNING_POLICY_VERSION,
            "project": normalized_project,
            "source_job_id": normalized_job,
            "decision": normalized_decision,
            "reviewer": normalized_reviewer,
            "review_reason": normalized_reason,
            "prompt_version": normalized_prompt_version,
            "model_digest": normalized_model_digest,
            "input_digest": input_digest,
            "prompt_digest": prompt_digest,
            "output_digest": output_digest,
            "parameters_digest": parameters_digest,
            "validation": safe_validation,
            "provenance": safe_provenance,
            "safety": json.loads(safety_json),
        }
        record_digest = _sha256(_canonical_json(core))
        record_id = f"learn_{record_digest[:32]}"
        reviewed_at = _utc_now(self._clock())
        row = {
            "record_id": record_id,
            "project": normalized_project,
            "source_job_id": normalized_job,
            "decision": normalized_decision,
            "dataset_kind": dataset_kind,
            "reviewer": normalized_reviewer,
            "review_reason": normalized_reason,
            "prompt_version": normalized_prompt_version,
            "model_digest": normalized_model_digest,
            "input_digest": input_digest,
            "prompt_digest": prompt_digest,
            "output_digest": output_digest,
            "parameters_digest": parameters_digest,
            "input_json": input_json,
            "prompt_json": prompt_json,
            "output_json": output_json,
            "parameters_json": parameters_json,
            "validation_json": validation_json,
            "provenance_json": provenance_json,
            "safety_json": safety_json,
            "record_digest": record_digest,
            "reviewed_at": reviewed_at,
        }
        self.initialize()

        def write() -> bool:
            with closing(self._connect()) as connection:
                existing = connection.execute(
                    "SELECT record_digest FROM llm_learning_reviews WHERE project=? AND source_job_id=?",
                    (normalized_project, normalized_job),
                ).fetchone()
                if existing is not None:
                    if str(existing["record_digest"]) == record_digest:
                        return False
                    raise LLMLearningCollision(normalized_job)
                count = int(connection.execute("SELECT COUNT(*) FROM llm_learning_reviews").fetchone()[0])
                if count >= self.max_records:
                    raise LLMLearningStoreFull(f"learning store capacity reached: {self.max_records}")
                connection.execute(
                    """
                    INSERT INTO llm_learning_reviews (
                        record_id, project, source_job_id, decision, dataset_kind,
                        reviewer, review_reason, prompt_version, model_digest,
                        input_digest, prompt_digest, output_digest, parameters_digest,
                        input_json, prompt_json, output_json, parameters_json,
                        validation_json, provenance_json, safety_json, record_digest,
                        reviewed_at
                    ) VALUES (
                        :record_id, :project, :source_job_id, :decision, :dataset_kind,
                        :reviewer, :review_reason, :prompt_version, :model_digest,
                        :input_digest, :prompt_digest, :output_digest, :parameters_digest,
                        :input_json, :prompt_json, :output_json, :parameters_json,
                        :validation_json, :provenance_json, :safety_json, :record_digest,
                        :reviewed_at
                    )
                    """,
                    row,
                )
                connection.commit()
                return True

        inserted = self._with_write_retry(write, priority=True)
        return {**_public_row(row), "inserted": bool(inserted)}

    def status(self, *, project: str | None = None) -> dict[str, Any]:
        self.initialize()
        normalized_project = _normalize_optional_project(project)
        with closing(self._connect()) as connection:
            where = " WHERE project=?" if normalized_project else ""
            args = (normalized_project,) if normalized_project else ()
            total = int(connection.execute(f"SELECT COUNT(*) FROM llm_learning_reviews{where}", args).fetchone()[0])
            accepted = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM llm_learning_reviews{where}{' AND' if where else ' WHERE'} decision='accepted'",
                    args,
                ).fetchone()[0]
            )
            rejected = int(
                connection.execute(
                    f"SELECT COUNT(*) FROM llm_learning_reviews{where}{' AND' if where else ' WHERE'} decision='rejected'",
                    args,
                ).fetchone()[0]
            )
            latest = connection.execute(
                f"SELECT reviewed_at FROM llm_learning_reviews{where} ORDER BY reviewed_at DESC LIMIT 1",
                args,
            ).fetchone()
        return {
            "schema_version": LLM_LEARNING_POLICY_VERSION,
            "path": str(self.path),
            "project": normalized_project,
            "max_records": self.max_records,
            "records": total,
            "accepted": accepted,
            "rejected": rejected,
            "eval_examples": accepted,
            "few_shot_examples": accepted,
            "regression_cases": rejected,
            "reviewed_only": True,
            "unverified_outputs_excluded": True,
            "raw_values_stored": False,
            "training": {
                "lora_enabled": False,
                "qlora_enabled": False,
                "training_started": False,
                "eligible": False,
                "requires_curated_dataset": True,
                "requires_baseline": True,
                "reason": "explicit_training_gate_not_implemented",
            },
            "latest_reviewed_at": None if latest is None else str(latest["reviewed_at"]),
            "writes_performed": False,
            "execution_enabled": False,
            "auto_apply": False,
        }

    def curate_dataset(
        self,
        *,
        project: str,
        limit: int = LLM_LEARNING_MAX_DATASET_RECORDS,
        include_payload: bool = True,
    ) -> dict[str, Any]:
        self.initialize()
        normalized_project = _normalize_token(project, "project", 120).casefold()
        bounded_limit = min(max(int(limit), 1), LLM_LEARNING_MAX_DATASET_RECORDS)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT * FROM llm_learning_reviews
                WHERE project=?
                ORDER BY reviewed_at ASC, record_id ASC
                LIMIT ?
                """,
                (normalized_project, bounded_limit),
            ).fetchall()
        eval_examples: list[dict[str, Any]] = []
        few_shot_examples: list[dict[str, Any]] = []
        regression_cases: list[dict[str, Any]] = []
        digests: list[str] = []
        for row in rows:
            digests.append(str(row["record_digest"]))
            if str(row["decision"]) == "accepted":
                example = {
                    "record_id": str(row["record_id"]),
                    "source_job_id": str(row["source_job_id"]),
                    "input_digest": str(row["input_digest"]),
                    "prompt_digest": str(row["prompt_digest"]),
                    "output_digest": str(row["output_digest"]),
                    "prompt_version": str(row["prompt_version"]),
                    "model_digest": str(row["model_digest"]),
                    "validation": json.loads(str(row["validation_json"])),
                    "provenance": json.loads(str(row["provenance_json"])),
                    "reviewer": str(row["reviewer"]),
                    "reviewed_at": str(row["reviewed_at"]),
                }
                if include_payload:
                    example.update(
                        {
                            "input": json.loads(str(row["input_json"])),
                            "prompt": json.loads(str(row["prompt_json"])),
                            "output": json.loads(str(row["output_json"])),
                        }
                    )
                eval_examples.append(dict(example))
                few_shot_examples.append(dict(example))
            else:
                regression = {
                    "record_id": str(row["record_id"]),
                    "source_job_id": str(row["source_job_id"]),
                    "input_digest": str(row["input_digest"]),
                    "prompt_digest": str(row["prompt_digest"]),
                    "output_digest": str(row["output_digest"]),
                    "failure_reason": str(row["review_reason"]),
                    "validation": json.loads(str(row["validation_json"])),
                    "provenance": json.loads(str(row["provenance_json"])),
                    "reviewer": str(row["reviewer"]),
                    "reviewed_at": str(row["reviewed_at"]),
                }
                if include_payload:
                    regression.update(
                        {
                            "input": json.loads(str(row["input_json"])),
                            "prompt": json.loads(str(row["prompt_json"])),
                            "actual_output": json.loads(str(row["output_json"])),
                        }
                    )
                regression_cases.append(regression)
        dataset_digest = _sha256(
            _canonical_json(
                {
                    "schema_version": LLM_LEARNING_POLICY_VERSION,
                    "project": normalized_project,
                    "records": digests,
                }
            )
        )
        return {
            "schema_version": LLM_LEARNING_POLICY_VERSION,
            "project": normalized_project,
            "dataset_digest": dataset_digest,
            "record_count": len(rows),
            "accepted_count": len(eval_examples),
            "rejected_count": len(regression_cases),
            "eval_examples": eval_examples,
            "few_shot_examples": few_shot_examples,
            "regression_cases": regression_cases,
            "curation": {
                "reviewed_only": True,
                "unverified_outputs_excluded": True,
                "raw_inputs_stored": False,
                "raw_prompts_stored": False,
                "raw_outputs_stored": False,
                "sanitized_payloads_only": True,
                "include_payload": bool(include_payload),
            },
            "training": {
                "lora_enabled": False,
                "qlora_enabled": False,
                "training_started": False,
                "eligible": False,
                "requires_baseline": True,
                "reason": "explicit_training_gate_not_implemented",
            },
            "writes_performed": False,
            "execution_enabled": False,
            "auto_apply": False,
        }

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _with_write_retry(self, operation: Callable[[], Any], *, priority: bool = False) -> Any:
        delays = (0.0, 0.0) + LLM_LEARNING_WRITE_RETRY_DELAYS if priority else LLM_LEARNING_WRITE_RETRY_DELAYS
        last_error: Exception | None = None
        for delay in delays:
            if delay:
                time.sleep(delay)
            try:
                return operation()
            except sqlite3.OperationalError as exc:
                message = str(exc).casefold()
                if "locked" not in message and "busy" not in message:
                    raise
                last_error = exc
        raise LLMLearningError("SQLite remained locked during learning write") from last_error


def default_llm_learning_path() -> Path:
    configured = str(os.getenv("BHM_LLM_LEARNING_PATH") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).resolve().parents[2] / ".runtime" / "llm-jobs" / "learning.sqlite3"


def _sanitize(value: Any, *, source: str, project: str, max_bytes: int) -> tuple[Any, dict[str, Any]]:
    try:
        transform = sanitize_llm_value(
            value,
            source=source,
            project=project,
            max_input_bytes=max_bytes,
            max_sanitized_bytes=max_bytes,
        )
    except LLMSafetyViolation as exc:
        raise LLMLearningBoundsError(str(exc)) from exc
    return transform.value, transform.provenance


def _validation_gate(validation: Mapping[str, Any]) -> tuple[bool, int]:
    checks = validation.get("checks")
    if not isinstance(checks, list):
        return False, 0
    bounded = checks[:32]
    passed = bool(validation.get("passed") is True and bounded)
    passed = passed and all(isinstance(item, Mapping) and item.get("passed") is True for item in bounded)
    return passed, len(bounded)


def _public_row(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": LLM_LEARNING_POLICY_VERSION,
        "record_id": str(row["record_id"]),
        "project": str(row["project"]),
        "source_job_id": str(row["source_job_id"]),
        "decision": str(row["decision"]),
        "dataset_kind": str(row["dataset_kind"]),
        "reviewer": str(row["reviewer"]),
        "review_reason": str(row["review_reason"]),
        "prompt_version": str(row["prompt_version"]),
        "model_digest": str(row["model_digest"]),
        "input_digest": str(row["input_digest"]),
        "prompt_digest": str(row["prompt_digest"]),
        "output_digest": str(row["output_digest"]),
        "parameters_digest": str(row["parameters_digest"]),
        "record_digest": str(row["record_digest"]),
        "reviewed_at": str(row["reviewed_at"]),
        "authority": "reviewed-proposal",
        "training_started": False,
        "auto_apply": False,
    }


def _normalize_decision(value: Any) -> str:
    normalized = str(value or "").strip().casefold()
    if normalized not in {"accepted", "rejected"}:
        raise LLMLearningReviewError("decision must be accepted or rejected")
    return normalized


def _normalize_optional_project(value: str | None) -> str | None:
    if value is None:
        return None
    return _normalize_token(value, "project", 120).casefold()


def _normalize_token(value: Any, field: str, max_length: int) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise LLMLearningError(f"{field} is required")
    if len(normalized) > max_length:
        raise LLMLearningBoundsError(f"{field} exceeds {max_length} characters")
    return normalized


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str, allow_nan=False)


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple, set)):
        return " ".join(_flatten_text(item) for item in value)
    return ""


def _sha256(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _utc_now(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "LLM_LEARNING_MAX_DATASET_RECORDS",
    "LLM_LEARNING_MAX_RECORDS",
    "LLM_LEARNING_POLICY_VERSION",
    "LLM_LEARNING_SCHEMA_VERSION",
    "LLMLearningBoundsError",
    "LLMLearningCollision",
    "LLMLearningError",
    "LLMLearningPrivacyError",
    "LLMLearningReviewError",
    "LLMLearningStore",
    "LLMLearningStoreFull",
    "default_llm_learning_path",
]
