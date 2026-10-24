"""SQLite persistence adapter for governed consolidation proposals."""

from __future__ import annotations

import copy
import json
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Mapping

from .governed_consolidation_base import (
    GOVERNED_CONSOLIDATION_CAPABILITY_KEY, GOVERNED_CONSOLIDATION_CAPABILITY_VERSION, MAX_PROPOSALS, STATUSES,
    GovernedConsolidationApprovalRequired, GovernedConsolidationError, GovernedConsolidationMigrationRequired,
    _bounded_items, _bounded_text, _canonical_json, _is_semantic_proposal,
    _semantic_attestation_error, _sha256, _utc_now_iso, validate_proposal_identity, validate_semantic_operator_attestation,
)
from .filesystem_boundaries import assert_safe_path

class GovernedConsolidationRepository:
    """SQLite authority adapter for proposal state, never canonical memories."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path).expanduser()

    def _connect(self) -> sqlite3.Connection:
        assert_safe_path(self.path)
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_ready(self, connection: sqlite3.Connection) -> None:
        try:
            row = connection.execute(
                "SELECT value FROM memory_store_meta WHERE key = ?",
                (GOVERNED_CONSOLIDATION_CAPABILITY_KEY,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise GovernedConsolidationMigrationRequired("governed consolidation schema is not installed") from exc
        if row is None or str(row["value"]) != GOVERNED_CONSOLIDATION_CAPABILITY_VERSION:
            raise GovernedConsolidationMigrationRequired("governed consolidation schema migration is required")

    def _write(self):
        class _Transaction:
            def __init__(self, outer: "GovernedConsolidationRepository") -> None:
                self.outer = outer
                self.connection: sqlite3.Connection | None = None

            def __enter__(self) -> sqlite3.Connection:
                self.connection = self.outer._connect()
                self.outer._ensure_ready(self.connection)
                self.connection.execute("BEGIN IMMEDIATE")
                return self.connection

            def __exit__(self, exc_type, exc, _tb) -> bool:
                assert self.connection is not None
                if exc_type is None:
                    self.connection.commit()
                else:
                    self.connection.rollback()
                self.connection.close()
                return False

        return _Transaction(self)

    def _read(self) -> sqlite3.Connection:
        connection = self._connect()
        self._ensure_ready(connection)
        return connection

    @staticmethod
    def _row_to_proposal(row: sqlite3.Row) -> dict[str, Any]:
        proposal = json.loads(str(row["proposal_json"]))
        proposal["status"] = str(row["status"])
        proposal["approval"] = {
            "approved_at": row["approved_at"],
            "approved_by_digest": row["approved_by_digest"],
            "rejected_at": row["rejected_at"],
            "rejected_by_digest": row["rejected_by_digest"],
            "applied_at": row["applied_at"],
            "stale_at": row["stale_at"],
            "failure_code": row["failure_code"],
        }
        return proposal

    def create(self, proposal: Mapping[str, Any]) -> tuple[dict[str, Any], bool]:
        normalized = copy.deepcopy(dict(proposal))
        if normalized.get("status") != "proposed" or normalized.get("requires_human_approval") is not True:
            raise GovernedConsolidationError("only a proposed human-approved contract can be stored")
        validate_proposal_identity(normalized)
        proposal_id = _bounded_text(normalized.get("proposal_id"), limit=96, field="proposal_id", required=True)
        proposal_digest = _bounded_text(normalized.get("proposal_digest"), limit=64, field="proposal_digest", required=True)
        project = _bounded_text(normalized.get("project"), limit=160, field="project", required=True)
        if len(proposal_digest) != 64:
            raise GovernedConsolidationError("proposal_digest must be SHA-256")
        proposal_json = _canonical_json(normalized)
        now = _utc_now_iso()
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM governed_consolidation_proposals WHERE project = ? AND proposal_digest = ?",
                (project, proposal_digest),
            ).fetchone()
            if existing is not None:
                return self._row_to_proposal(existing), False
            connection.execute(
                """
                INSERT INTO governed_consolidation_proposals(
                    proposal_id, proposal_digest, project, operation, status, basis_json,
                    candidate_json, reason, confidence, conflicts_json, provenance_json,
                    proposal_json, requires_human_approval, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'proposed', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    proposal_id, proposal_digest, project, normalized["operation"],
                    _canonical_json(normalized["basis"]), _canonical_json(normalized["candidate"]),
                    normalized["reason"], normalized["confidence"], _canonical_json(normalized["conflicts"]),
                    _canonical_json(normalized["provenance"]), proposal_json, now, now,
                ),
            )
            self._append_event(connection, proposal_id, "created", now, {"proposal_digest": proposal_digest})
        return self.get(proposal_id, project=project), True

    def get(self, proposal_id: str, *, project: str | None = None) -> dict[str, Any]:
        connection = self._read()
        try:
            where = "proposal_id = ?" + (" AND project = ?" if project else "")
            params: tuple[Any, ...] = (proposal_id,) + ((project,) if project else ())
            row = connection.execute(f"SELECT * FROM governed_consolidation_proposals WHERE {where}", params).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            return self._row_to_proposal(row)
        finally:
            connection.close()


    def list(self, *, project: str, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if not 1 <= int(limit) <= MAX_PROPOSALS:
            raise GovernedConsolidationError(f"limit must be within 1..{MAX_PROPOSALS}")
        if status is not None and status not in STATUSES:
            raise GovernedConsolidationError("unsupported proposal status")
        connection = self._read()
        try:
            if status:
                rows = connection.execute(
                    "SELECT * FROM governed_consolidation_proposals WHERE project = ? AND status = ? ORDER BY created_at DESC, proposal_id LIMIT ?",
                    (project, status, int(limit)),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM governed_consolidation_proposals WHERE project = ? ORDER BY created_at DESC, proposal_id LIMIT ?",
                    (project, int(limit)),
                ).fetchall()
            return [self._row_to_proposal(row) for row in rows]
        finally:
            connection.close()

    def count_by_status(self) -> dict[str, int]:
        connection = self._read()
        try:
            counts = {str(row[0]): int(row[1]) for row in connection.execute("SELECT status, COUNT(*) FROM governed_consolidation_proposals GROUP BY status")}
            return {status: counts.get(status, 0) for status in sorted(STATUSES)}
        finally:
            connection.close()

    def decide(
        self,
        *,
        proposal_id: str,
        project: str,
        decision: str,
        actor: str,
        semantic_attestation: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if decision not in {"approve", "reject"}:
            raise GovernedConsolidationError("decision must be approve or reject")
        actor_digest = _sha256(_bounded_text(actor, limit=240, field="actor", required=True))
        now = _utc_now_iso()
        with self._write() as connection:
            row = connection.execute(
                "SELECT status, proposal_json FROM governed_consolidation_proposals WHERE proposal_id = ? AND project = ?",
                (proposal_id, project),
            ).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            proposal = json.loads(str(row["proposal_json"]))
            validate_proposal_identity(proposal)
            status = str(row["status"])
            desired = "approved" if decision == "approve" else "rejected"
            if status == desired:
                if decision == "approve" and _is_semantic_proposal(proposal):
                    supplied = validate_semantic_operator_attestation(
                        semantic_attestation,
                        proposal=proposal,
                    )
                    recorded = self._validate_semantic_approval_event(
                        connection,
                        proposal_id=proposal_id,
                        proposal=proposal,
                    )
                    if supplied != recorded:
                        _semantic_attestation_error("approval event does not match supplied attestation")
                return self.get(proposal_id, project=project)
            if status != "proposed":
                raise GovernedConsolidationError(f"proposal cannot be {decision}d from {status}")
            validated_attestation = None
            if decision == "approve" and _is_semantic_proposal(proposal):
                validated_attestation = validate_semantic_operator_attestation(
                    semantic_attestation,
                    proposal=proposal,
                )
            column = "approved" if decision == "approve" else "rejected"
            connection.execute(
                f"UPDATE governed_consolidation_proposals SET status = ?, {column}_at = ?, {column}_by_digest = ?, updated_at = ? WHERE proposal_id = ?",
                (desired, now, actor_digest, now, proposal_id),
            )
            details: dict[str, Any] = {"actor_digest": actor_digest}
            if validated_attestation is not None:
                details["semantic_operator_attestation"] = validated_attestation
            self._append_event(connection, proposal_id, desired, now, details)
        return self.get(proposal_id, project=project)

    @staticmethod
    def _validate_semantic_approval_event(
        connection: sqlite3.Connection,
        *,
        proposal_id: str,
        proposal: Mapping[str, Any],
    ) -> dict[str, Any]:
        """Require exactly one valid manual semantic approval event before apply."""

        rows = connection.execute(
            "SELECT details_json FROM governed_consolidation_events WHERE proposal_id = ? AND action = 'approved' ORDER BY created_at, event_id",
            (proposal_id,),
        ).fetchall()
        if len(rows) != 1:
            _semantic_attestation_error("exactly one approval event is required")
        try:
            details = json.loads(str(rows[0]["details_json"]))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            _semantic_attestation_error("approval event is malformed")
            raise AssertionError("unreachable") from exc
        if not isinstance(details, Mapping):
            _semantic_attestation_error("approval event is malformed")
        return validate_semantic_operator_attestation(
            details.get("semantic_operator_attestation"),
            proposal=proposal,
        )

    def validate_approved_semantic_attestation(self, proposal: Mapping[str, Any]) -> None:
        """Read-only event check used immediately before an approved semantic apply."""

        if not _is_semantic_proposal(proposal):
            return
        connection = self._read()
        try:

            self._validate_semantic_approval_event(
                connection,
                proposal_id=str(proposal["proposal_id"]),
                proposal=proposal,
            )
        finally:
            connection.close()

    def decide_automatically(
        self,
        *,
        proposal_id: str,
        project: str,
        decision: str,
        actor: str,
        policy_version: str,
        reason_codes: Iterable[str],
    ) -> dict[str, Any]:
        """Persist one deterministic policy decision without storing model text.

        This is deliberately separate from the operator decision surface.  The
        resulting proposal status is identical, while the event receipt makes
        the automated actor, policy version and bounded reason codes explicit.
        """

        if decision not in {"approve", "reject"}:
            raise GovernedConsolidationError("decision must be approve or reject")
        actor_digest = _sha256(_bounded_text(actor, limit=240, field="actor", required=True))
        normalized_policy = _bounded_text(policy_version, limit=120, field="policy_version", required=True)
        normalized_reasons = _bounded_items(list(reason_codes), limit=12, field="reason_codes")
        if not normalized_reasons:
            raise GovernedConsolidationError("reason_codes is required")
        now = _utc_now_iso()
        with self._write() as connection:
            row = connection.execute(
                "SELECT status, proposal_json FROM governed_consolidation_proposals WHERE proposal_id = ? AND project = ?",
                (proposal_id, project),
            ).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            proposal = json.loads(str(row["proposal_json"]))
            validate_proposal_identity(proposal)
            if decision == "approve" and _is_semantic_proposal(proposal):
                raise GovernedConsolidationApprovalRequired(
                    "semantic proposals require a privileged operator attestation"
                )
            status = str(row["status"])
            desired = "approved" if decision == "approve" else "rejected"
            if status == desired:
                return self.get(proposal_id, project=project)
            if status != "proposed":
                raise GovernedConsolidationError(f"proposal cannot be automatically {decision}d from {status}")
            column = "approved" if decision == "approve" else "rejected"
            connection.execute(
                f"UPDATE governed_consolidation_proposals SET status = ?, {column}_at = ?, {column}_by_digest = ?, updated_at = ? WHERE proposal_id = ?",
                (desired, now, actor_digest, now, proposal_id),
            )
            self._append_event(
                connection,
                proposal_id,
                desired,
                now,
                {
                    "actor_digest": actor_digest,
                    "automatic": True,
                    "policy_version": normalized_policy,
                    "reason_codes": normalized_reasons,
                },
            )
        return self.get(proposal_id, project=project)

    @staticmethod
    def _append_event(connection: sqlite3.Connection, proposal_id: str, action: str, now: str, details: Mapping[str, Any]) -> None:
        event_digest = _sha256(_canonical_json({"proposal_id": proposal_id, "action": action, "at": now, "details": dict(details)}))
        connection.execute(
            "INSERT INTO governed_consolidation_events(event_id, proposal_id, action, details_json, created_at) VALUES (?, ?, ?, ?, ?)",
            (f"gce_bhm_{event_digest[:24]}", proposal_id, action, _canonical_json(dict(details)), now),
        )

    def mark_stale(self, *, proposal_id: str, project: str, reason: str) -> None:
        now = _utc_now_iso()
        with self._write() as connection:
            row = connection.execute(
                "SELECT status FROM governed_consolidation_proposals WHERE proposal_id = ? AND project = ?",
                (proposal_id, project),
            ).fetchone()
            if row is None:
                raise GovernedConsolidationError("proposal not found")
            if str(row["status"]) in {"applied", "rejected"}:
                return
            connection.execute(
                "UPDATE governed_consolidation_proposals SET status = 'stale', stale_at = ?, failure_code = ?, updated_at = ? WHERE proposal_id = ?",
                (now, _bounded_text(reason, limit=160, field="reason", required=True), now, proposal_id),
            )
            self._append_event(connection, proposal_id, "stale", now, {"reason": reason})


