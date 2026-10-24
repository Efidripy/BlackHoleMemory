"""Repository index store operations."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Mapping, Sequence

from .repository_index_types import (
    RepositoryIndexError,
    RepositorySkip,
    RepositorySourceProvenance,
    RepositoryState,
    _canonical_json,
    _clip,
    _sha256_json,
    _utc_now,
)

_LOGGER = logging.getLogger(__name__)

class _RepositoryIndexStoreJobsMixin:
    def _transaction(self) -> sqlite3.Connection:
        self.initialize()
        connection = self._connect()
        self._begin_immediate(connection)
        return connection

    @staticmethod
    def _row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def begin_or_resume_job(
        self,
        state: RepositoryState,
        source: RepositorySourceProvenance,
        *,
        started_at: str | None = None,
        force_refresh_nonce: str | None = None,
    ) -> dict[str, Any]:
        source_payload = source.as_dict()
        if force_refresh_nonce:
            # A forced refresh is an explicit operator epoch.  Binding the
            # nonce into the source digest makes the resulting snapshot
            # immutable and freshness-visible without changing file content.
            source_payload["refresh_nonce"] = str(force_refresh_nonce)
        source_json = _canonical_json(source_payload)
        job_key = {
            "project": state.project,
            "root_id": state.root_id,
            "state_digest": state.state_digest,
            "config_digest": state.config_digest,
            "source": source_payload,
        }
        job_id = f"job_bhm_{_sha256_json(job_key)[:24]}"
        now = started_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                row = connection.execute(
                    "SELECT * FROM repository_index_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if not force_refresh_nonce and (row is None or row["status"] == "completed"):
                    # A bounded forced refresh may have been interrupted after
                    # one batch.  Resume that running epoch instead of creating
                    # a fresh job that can accidentally republish its prior
                    # snapshot and digest.
                    running_row = connection.execute(
                        """
                        SELECT * FROM repository_index_jobs
                        WHERE project = ? AND root_id = ? AND state_digest = ?
                          AND config_digest = ? AND status = 'running'
                        ORDER BY updated_at DESC, job_id
                        LIMIT 1
                        """,
                        (state.project, state.root_id, state.state_digest, state.config_digest),
                    ).fetchone()
                    if running_row is not None:
                        row = running_row
                        job_id = str(row["job_id"])
                    else:
                        # A forced refresh creates a distinct immutable job for
                        # the same repository state.  Prefer the newest
                        # completed epoch so a later non-force call cannot
                        # republish the older deterministic job and move the
                        # authoritative current pointer backwards.
                        completed_row = connection.execute(
                            """
                            SELECT * FROM repository_index_jobs
                            WHERE project = ? AND root_id = ? AND state_digest = ?
                              AND config_digest = ? AND status = 'completed'
                            ORDER BY completed_at DESC, updated_at DESC, job_id DESC
                            LIMIT 1
                            """,
                            (state.project, state.root_id, state.state_digest, state.config_digest),
                        ).fetchone()
                        row = completed_row
                        if row is not None:
                            job_id = str(row["job_id"])
                if row is None:
                    connection.execute(
                        """
                        INSERT INTO repository_index_jobs(
                            job_id, project, root_id, root_path, state_digest,
                            config_digest, candidate_digest, git_status_sha256, git_head, git_branch,
                            dirty, source_json, status, total_candidates,
                            prefiltered_skip_count, started_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running', ?, ?, ?, ?)
                        """,
                        (
                            job_id,
                            state.project,
                            state.root_id,
                            str(state.root),
                            state.state_digest,
                            state.config_digest,
                            state.candidate_digest,
                            state.git_status_sha256,
                            state.git_head,
                            state.git_branch,
                            int(state.dirty),
                            source_json,
                            len(state.candidates),
                            len(state.prefiltered_skips),
                            now,
                            now,
                        ),
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_job_candidates(
                            job_id, path, size_bytes, mtime_ns, origin
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        [
                            (job_id, item.path, item.size_bytes, item.mtime_ns, item.origin)
                            for item in state.candidates
                        ],
                    )
                    connection.executemany(
                        """
                        INSERT INTO repository_index_job_skips(job_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(job_id, item.path, item.reason, item.size_bytes) for item in state.prefiltered_skips],
                    )
                elif row["status"] != "completed":
                    connection.execute(
                        """
                        UPDATE repository_index_jobs
                        SET status = 'running', updated_at = ?, error_code = NULL, error_detail = NULL
                        WHERE job_id = ?
                        """,
                        (now, job_id),
                    )
                connection.commit()
            except Exception:  # broad: preserve rollback for any job transaction failure
                _LOGGER.error("repository index job transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def job(self, job_id: str) -> dict[str, Any]:
        self.initialize()
        connection = self._connect(read_only=True)
        try:
            row = connection.execute("SELECT * FROM repository_index_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise RepositoryIndexError(f"repository index job not found: {job_id}")
            return dict(row)
        finally:
            connection.close()

    def running_jobs(
        self,
        project: str,
        root_id: str,
        *,
        limit: int = 16,
    ) -> list[dict[str, Any]]:
        """Read running index jobs without creating or mutating the store.

        Watcher backpressure is deliberately operator-managed.  This probe is
        fail-closed for an absent/unready index database and returns bounded
        metadata only; it never opens a write transaction or starts a worker.
        """

        if not 1 <= int(limit) <= 64:
            raise ValueError("limit must be between 1 and 64")
        if not self.path.is_file():
            return []
        schema = self.inspect_schema()
        if not schema["ready"]:
            return []
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT job_id, project, root_id, state_digest, status,
                       total_candidates, processed_count, updated_at
                FROM repository_index_jobs
                WHERE project = ? AND root_id = ? AND status = 'running'
                ORDER BY updated_at DESC, job_id
                LIMIT ?
                """,
                (_clip(project, 120), _clip(root_id, 180), int(limit)),
            ).fetchall()
            return [dict(row) for row in rows]
        finally:
            connection.close()

    def processed_paths(self, job_id: str) -> set[str]:
        self.initialize()
        connection = self._connect(read_only=True)
        try:
            rows = connection.execute(
                """
                SELECT path FROM repository_index_job_files WHERE job_id = ?
                UNION
                SELECT path FROM repository_index_job_skips WHERE job_id = ?
                """,
                (job_id, job_id),
            ).fetchall()
            return {str(row["path"]) for row in rows}
        finally:
            connection.close()

    def save_batch(
        self,
        job_id: str,
        files: Sequence[Mapping[str, Any]],
        skips: Sequence[RepositorySkip],
        *,
        cursor_path: str | None,
        reused_count_delta: int = 0,
        updated_at: str | None = None,
    ) -> dict[str, Any]:
        now = updated_at or _utc_now()
        with self._write_lock:
            connection = self._transaction()
            try:
                row = connection.execute(
                    "SELECT status FROM repository_index_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
                if row is None or row["status"] != "running":
                    raise RepositoryIndexError(f"job is not writable: {job_id}")
                if files:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO repository_index_job_files(
                            job_id, path, content_sha256, size_bytes, line_count,
                            mtime_ns, language, file_kind, origin
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        [
                            (
                                job_id,
                                str(item["path"]),
                                str(item["content_sha256"]),
                                int(item["size_bytes"]),
                                int(item["line_count"]),
                                int(item["mtime_ns"]),
                                str(item["language"]),
                                str(item["file_kind"]),
                                str(item["origin"]),
                            )
                            for item in files
                        ],
                    )
                if skips:
                    connection.executemany(
                        """
                        INSERT OR REPLACE INTO repository_index_job_skips(job_id, path, reason, size_bytes)
                        VALUES (?, ?, ?, ?)
                        """,
                        [(job_id, item.path, item.reason, item.size_bytes) for item in skips],
                    )
                indexed_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM repository_index_job_files WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                skipped_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM repository_index_job_skips WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                processed_count = indexed_count + max(
                    0,
                    skipped_count
                    - int(
                        connection.execute(
                            "SELECT prefiltered_skip_count FROM repository_index_jobs WHERE job_id = ?",
                            (job_id,),
                        ).fetchone()[0]
                    ),
                )
                existing_reused = int(
                    connection.execute(
                        "SELECT reused_count FROM repository_index_jobs WHERE job_id = ?",
                        (job_id,),
                    ).fetchone()[0]
                )
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET processed_count = ?, indexed_count = ?, skipped_count = ?,
                        reused_count = ?, cursor_path = ?, updated_at = ?
                    WHERE job_id = ?
                    """,
                    (
                        processed_count,
                        indexed_count,
                        skipped_count,
                        existing_reused + max(int(reused_count_delta), 0),
                        cursor_path,
                        now,
                        job_id,
                    ),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for any batch transaction failure
                _LOGGER.error("repository index batch transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)

    def mark_failed(self, job_id: str, *, code: str, detail: str) -> dict[str, Any]:
        with self._write_lock:
            connection = self._transaction()
            try:
                connection.execute(
                    """
                    UPDATE repository_index_jobs
                    SET status = 'failed', error_code = ?, error_detail = ?, updated_at = ?
                    WHERE job_id = ? AND status = 'running'
                    """,
                    (_clip(code, 120), _clip(detail, 1_000), _utc_now(), job_id),
                )
                connection.commit()
            except Exception:  # broad: preserve rollback for any failure-state transaction
                _LOGGER.error("repository index failure-state transaction failed; rolling back")
                connection.rollback()
                raise
            finally:
                connection.close()
        return self.job(job_id)



