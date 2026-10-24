"""Repository index store operations."""

from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any

from .repository_index_snapshots import (
    _build_delta,
    _build_snapshot_summary,
    _snapshot_digest_files,
)
from .repository_index_types import (
    REPOSITORY_INDEX_SCHEMA_VERSION,
    RepositoryIndexError,
    _canonical_json,
    _sha256_json,
    _utc_now,
    repository_index_error_retryable,
)

_LOGGER = logging.getLogger(__name__)

class _RepositoryIndexStoreSnapshotsMixin:
    def current_snapshot(self, project: str, root_id: str, *, include_files: bool = False) -> dict[str, Any] | None:
        schema = self.inspect_schema(fast=True)
        if not schema["ready"]:
            return None
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                """
                SELECT snapshots.*
                FROM repository_index_current AS current
                JOIN repository_index_snapshots AS snapshots
                  ON snapshots.snapshot_id = current.snapshot_id
                WHERE current.project = ? AND current.root_id = ?
                """,
                (project, root_id),
            ).fetchone()
            if row is None:
                return None
            return self._snapshot_payload(connection, row, include_files=include_files)
        finally:
            connection.close()

    def promote_snapshot(
        self,
        snapshot_id: str,
        *,
        project: str,
        root_id: str,
        updated_at: str | None = None,
    ) -> None:
        """Repair the authoritative current pointer to an existing snapshot.

        A completed, content-addressed job may be safely reused after another
        operator moved the current pointer (for example, a stale cold-start
        watcher snapshot).  Re-publishing the already verified snapshot keeps
        the operation idempotent and does not re-read source or create a second
        snapshot.
        """
        snapshot = self.snapshot(snapshot_id, include_files=False)
        if str(snapshot.get("project")) != project or str(snapshot.get("root_id")) != root_id:
            raise RepositoryIndexError("snapshot ownership mismatch during pointer repair")
        now = updated_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                connection.execute(
                    """
                    INSERT INTO repository_index_current(project, root_id, snapshot_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project, root_id) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        updated_at = excluded.updated_at
                    """,
                    (project, root_id, snapshot_id, now),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for atomic pointer repair
                _LOGGER.error("repository index pointer-repair transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()

    def snapshot(self, snapshot_id: str, *, include_files: bool = False, read_only: bool = False) -> dict[str, Any]:
        if read_only:
            schema = self.inspect_schema(fast=True)
            if not schema["ready"]:
                raise RepositoryIndexError("repository index schema is not ready")
        else:
            self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute(
                "SELECT * FROM repository_index_snapshots WHERE snapshot_id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise RepositoryIndexError(f"repository snapshot not found: {snapshot_id}")
            return self._snapshot_payload(connection, row, include_files=include_files)
        finally:
            connection.close()

    @staticmethod
    def _snapshot_payload(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        include_files: bool,
    ) -> dict[str, Any]:
        payload = dict(row)
        payload["dirty"] = bool(payload["dirty"])
        payload["source"] = json.loads(str(payload.pop("source_json")))
        payload["summary"] = json.loads(str(payload.pop("summary_json")))
        payload["delta"] = json.loads(str(payload.pop("delta_json")))
        if include_files:
            payload["files"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM repository_index_snapshot_files WHERE snapshot_id = ? ORDER BY path",
                    (row["snapshot_id"],),
                ).fetchall()
            ]
            payload["skips"] = [
                dict(item)
                for item in connection.execute(
                    "SELECT path, reason, size_bytes FROM repository_index_snapshot_skips "
                    "WHERE snapshot_id = ? ORDER BY path, reason",
                    (row["snapshot_id"],),
                ).fetchall()
            ]
        return payload

    def _job_material(self, connection: sqlite3.Connection, job_id: str) -> tuple[sqlite3.Row, list[dict[str, Any]], list[dict[str, Any]]]:
        job = connection.execute("SELECT * FROM repository_index_jobs WHERE job_id = ?", (job_id,)).fetchone()
        if job is None:
            raise RepositoryIndexError(f"repository index job not found: {job_id}")
        files = [
            dict(row)
            for row in connection.execute(
                "SELECT path, content_sha256, size_bytes, line_count, mtime_ns, language, file_kind, origin "
                "FROM repository_index_job_files WHERE job_id = ? ORDER BY path",
                (job_id,),
            ).fetchall()
        ]
        skips = [
            dict(row)
            for row in connection.execute(
                "SELECT path, reason, size_bytes FROM repository_index_job_skips WHERE job_id = ? ORDER BY path, reason",
                (job_id,),
            ).fetchall()
        ]
        return job, files, skips

    def finalize_job(
        self,
        job_id: str,
        *,
        completed_at: str | None = None,
    ) -> dict[str, Any]:
        now = completed_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                job, files, skips = self._job_material(connection, job_id)
                if job["status"] == "completed" and job["snapshot_id"]:
                    connection.commit()
                    return self.snapshot(str(job["snapshot_id"]), include_files=False)
                if int(job["processed_count"]) != int(job["total_candidates"]):
                    raise RepositoryIndexError(
                        f"job is incomplete: {job['processed_count']}/{job['total_candidates']}"
                    )
                current = connection.execute(
                    "SELECT snapshot_id FROM repository_index_current WHERE project = ? AND root_id = ?",
                    (job["project"], job["root_id"]),
                ).fetchone()
                previous_snapshot_id = str(current["snapshot_id"]) if current is not None else None
                previous_files: list[dict[str, Any]] = []
                if previous_snapshot_id:
                    previous_files = [
                        dict(row)
                        for row in connection.execute(
                            "SELECT path, content_sha256, size_bytes, line_count, mtime_ns, language, file_kind, origin "
                            "FROM repository_index_snapshot_files WHERE snapshot_id = ? ORDER BY path",
                            (previous_snapshot_id,),
                        ).fetchall()
                    ]
                delta = _build_delta(previous_files, files)
                summary = _build_snapshot_summary(files, skips)
                source = json.loads(str(job["source_json"]))
                graph_input_digest = _sha256_json(
                    [
                        {
                            "path": item["path"],
                            "content_sha256": item["content_sha256"],
                            "language": item["language"],
                            "file_kind": item["file_kind"],
                        }
                        for item in files
                    ]
                )
                snapshot_core = {
                    "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                    "project": job["project"],
                    "root_id": job["root_id"],
                    "config_digest": job["config_digest"],
                    "git_head": job["git_head"],
                    "git_branch": job["git_branch"],
                    "dirty": bool(job["dirty"]),
                    "git_status_sha256": job["git_status_sha256"],
                    "state_digest": job["state_digest"],
                    "candidate_digest": job["candidate_digest"],
                    "source": source,
                    "files": _snapshot_digest_files(files),
                    "skips": skips,
                }
                snapshot_digest = _sha256_json(snapshot_core)
                snapshot_id = f"snapshot_bhm_{snapshot_digest[:24]}"
                existing = connection.execute(
                    "SELECT snapshot_digest FROM repository_index_snapshots WHERE snapshot_id = ?",
                    (snapshot_id,),
                ).fetchone()
                if existing is not None and str(existing["snapshot_digest"]) != snapshot_digest:
                    raise RepositoryIndexError(f"snapshot id collision: {snapshot_id}")
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO repository_index_snapshots(
                            snapshot_id, project, root_id, root_path, state_digest,
                            snapshot_digest, graph_input_digest, config_digest,
                            candidate_digest, git_status_sha256, git_head, git_branch,
                            dirty, previous_snapshot_id,
                            source_json, summary_json, delta_json, created_at, completed_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            snapshot_id,
                            job["project"],
                            job["root_id"],
                            job["root_path"],
                            job["state_digest"],
                            snapshot_digest,
                            graph_input_digest,
                            job["config_digest"],
                            job["candidate_digest"],
                            job["git_status_sha256"],
                            job["git_head"],
                            job["git_branch"],
                            job["dirty"],
                            previous_snapshot_id if previous_snapshot_id != snapshot_id else None,
                            job["source_json"],
                            _canonical_json(summary),
                            _canonical_json(delta),
                            job["started_at"],
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_snapshot_files(
                            snapshot_id, path, content_sha256, size_bytes,
                            line_count, mtime_ns, language, file_kind, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                snapshot_id,
                                item["path"],
                                item["content_sha256"],
                                item["size_bytes"],
                                item["line_count"],
                                item["mtime_ns"],
                                item["language"],
                                item["file_kind"],
                                item["origin"],
                            )
                            for item in files
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_snapshot_skips(snapshot_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(snapshot_id, item["path"], item["reason"], item["size_bytes"]) for item in skips],
                    )
                    revision = str(job["git_head"] or f"dirty:{snapshot_digest}")
                    source_import_id = f"source_import_bhm_{_sha256_json({'snapshot_id': snapshot_id, 'source': source})[:24]}"
                    connection.execute(
                        """
                        INSERT INTO repository_source_imports(
                            source_import_id, snapshot_id, project, root_id,
                            source_url, revision, license, evidence_class, owner,
                            source_registry_id, extraction_version, extracted_at,
                            content_sha256, provenance_json
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            source_import_id,
                            snapshot_id,
                            job["project"],
                            job["root_id"],
                            source["source_url"],
                            revision,
                            source["license"],
                            source["evidence_class"],
                            source["owner"],
                            source.get("source_registry_id"),
                            REPOSITORY_INDEX_SCHEMA_VERSION,
                            now,
                            snapshot_digest,
                            _canonical_json(
                                {
                                    "git_head": job["git_head"],
                                    "git_branch": job["git_branch"],
                                    "dirty": bool(job["dirty"]),
                                    "state_digest": job["state_digest"],
                                    "config_digest": job["config_digest"],
                                }
                            ),
                        ),
                    )
                connection.execute(
                    """
                    INSERT INTO repository_index_current(project, root_id, snapshot_id, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(project, root_id) DO UPDATE SET
                        snapshot_id = excluded.snapshot_id,
                        updated_at = excluded.updated_at
                    """,
                    (job["project"], job["root_id"], snapshot_id, now),
                )
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET status = 'completed', snapshot_id = ?, completed_at = ?,
                        updated_at = ?, error_code = NULL, error_detail = NULL
                    WHERE job_id = ?
                    """,
                    (snapshot_id, now, now, job_id),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for atomic snapshot publication
                _LOGGER.error("repository index snapshot-publication transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.snapshot(snapshot_id, include_files=False)

    def status(self, project: str, root_id: str, *, fast_schema: bool = False) -> dict[str, Any]:
        schema = self.inspect_schema(fast=fast_schema)
        if not schema["ready"]:
            return {
                "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                "database_path": str(self.path),
                "project": project,
                "root_id": root_id,
                "repository_schema": schema,
                "current_snapshot": None,
                "latest_job": None,
                "retryable": False,
            }
        current = self.current_snapshot(project, root_id, include_files=False)
        connection = self._connect(read_only=True)
        try:
            job = connection.execute(
                """
                SELECT * FROM repository_index_jobs
                WHERE project = ? AND root_id = ?
                ORDER BY updated_at DESC, job_id DESC LIMIT 1
                """,
                (project, root_id),
            ).fetchone()
            latest_job = dict(job) if job is not None else None
            retryable = False
            if latest_job is not None and latest_job.get("status") == "failed":
                retryable = repository_index_error_retryable(
                    RepositoryIndexError(str(latest_job.get("error_detail") or latest_job.get("error_code") or ""))
                )
            return {
                "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
                "database_path": str(self.path),
                "project": project,
                "root_id": root_id,
                "repository_schema": schema,
                "current_snapshot": current,
                "latest_job": latest_job,
                "retryable": retryable,
            }
        finally:
            connection.close()





