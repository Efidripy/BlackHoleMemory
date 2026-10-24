"""Deterministic repository watcher and incremental index foundation.

The index stores only bounded file metadata and content digests. Source text is
never persisted. Completed snapshots live in the same SQLite authority as BHM
memory records; graph extraction and retrieval publication are later gates.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Mapping

from .filesystem_boundaries import assert_safe_path
from .repository_index_discovery import (
    _enumerate_repository_paths as _enumerate_repository_paths,  # noqa: F401
    _git_path_list as _git_path_list,  # noqa: F401
    _kind_for_path as _kind_for_path,  # noqa: F401
    _language_for_path as _language_for_path,  # noqa: F401
    _normalize_relative_path as _normalize_relative_path,  # noqa: F401
    _path_filter_reason as _path_filter_reason,  # noqa: F401
    _read_candidate,
    _resolved_candidate as _resolved_candidate,  # noqa: F401
    _run_git as _run_git,  # noqa: F401
    probe_repository_state as probe_repository_state,  # noqa: F401
    repository_root_id as repository_root_id,  # noqa: F401
)
from .repository_index_snapshots import (
    _build_delta as _build_delta,  # noqa: F401
    _build_snapshot_summary as _build_snapshot_summary,  # noqa: F401
    _snapshot_digest_files as _snapshot_digest_files,  # noqa: F401
    verify_repository_snapshot as verify_repository_snapshot,  # noqa: F401
)
from .repository_index_store import SQLiteRepositoryIndexStore as SQLiteRepositoryIndexStore  # noqa: F401
from .repository_index_store_base import (
    RepositoryIndexStoreBase as RepositoryIndexStoreBase,  # noqa: F401
    _RepositoryIndexStoreBase as _RepositoryIndexStoreBase,  # noqa: F401
)
from .repository_index_types import (
    DEFAULT_BATCH_SIZE as DEFAULT_BATCH_SIZE,  # noqa: F401
    DEFAULT_MAX_CANDIDATES as DEFAULT_MAX_CANDIDATES,  # noqa: F401
    DEFAULT_MAX_FILE_BYTES as DEFAULT_MAX_FILE_BYTES,  # noqa: F401
    DEFAULT_MAX_TOTAL_BYTES as DEFAULT_MAX_TOTAL_BYTES,  # noqa: F401
    DEFAULT_WATCH_INTERVAL_SECONDS as DEFAULT_WATCH_INTERVAL_SECONDS,  # noqa: F401
    DEFAULT_WATCH_MAX_INFLIGHT_JOBS as DEFAULT_WATCH_MAX_INFLIGHT_JOBS,  # noqa: F401
    DEFAULT_WATCH_RETRY_ATTEMPTS as DEFAULT_WATCH_RETRY_ATTEMPTS,  # noqa: F401
    DEFAULT_WATCH_RETRY_BACKOFF_SECONDS as DEFAULT_WATCH_RETRY_BACKOFF_SECONDS,  # noqa: F401
    MAX_WATCH_MAX_INFLIGHT_JOBS as MAX_WATCH_MAX_INFLIGHT_JOBS,  # noqa: F401
    REPOSITORY_INDEX_BUSY_TIMEOUT_MS as REPOSITORY_INDEX_BUSY_TIMEOUT_MS,  # noqa: F401
    REPOSITORY_INDEX_REPORT_LIST_LIMIT,
    REPOSITORY_INDEX_SCHEMA_VERSION,
    REPOSITORY_INDEX_STORE_SCHEMA_VERSION as REPOSITORY_INDEX_STORE_SCHEMA_VERSION,  # noqa: F401
    REPOSITORY_INDEX_WRITE_RETRY_DELAYS as REPOSITORY_INDEX_WRITE_RETRY_DELAYS,  # noqa: F401
    STALE_JOB_DISPOSITION_APPLY_SCHEMA_VERSION as STALE_JOB_DISPOSITION_APPLY_SCHEMA_VERSION,  # noqa: F401
    STALE_JOB_DISPOSITION_ERROR_CODE as STALE_JOB_DISPOSITION_ERROR_CODE,  # noqa: F401
    STALE_JOB_DISPOSITION_PLAN_SCHEMA_VERSION as STALE_JOB_DISPOSITION_PLAN_SCHEMA_VERSION,  # noqa: F401
    STALE_JOB_DISPOSITION_REASON as STALE_JOB_DISPOSITION_REASON,  # noqa: F401
    RepositoryCandidate as RepositoryCandidate,  # noqa: F401
    RepositoryIndexError,
    RepositoryIndexInjectedFailure,
    RepositoryIndexLimits,
    RepositoryIndexMigrationRequired as RepositoryIndexMigrationRequired,  # noqa: F401
    RepositoryRootError,
    RepositorySkip as RepositorySkip,  # noqa: F401
    RepositorySourceProvenance,
    RepositoryState,
    RepositoryStateChangedError,
    _ALLOWED_SUFFIXES as _ALLOWED_SUFFIXES,  # noqa: F401
    _BLOCKED_PARTS as _BLOCKED_PARTS,  # noqa: F401
    _DATABASE_SUFFIXES as _DATABASE_SUFFIXES,  # noqa: F401
    _GENERATED_NAMES as _GENERATED_NAMES,  # noqa: F401
    _GENERATED_SUFFIXES as _GENERATED_SUFFIXES,  # noqa: F401
    _LANGUAGE_BY_SUFFIX as _LANGUAGE_BY_SUFFIX,  # noqa: F401
    _REPOSITORY_INDEX_TABLES as _REPOSITORY_INDEX_TABLES,  # noqa: F401
    _SECRET_PARTS as _SECRET_PARTS,  # noqa: F401
    _SECRET_SUFFIXES as _SECRET_SUFFIXES,  # noqa: F401
    _SPECIAL_TEXT_NAMES as _SPECIAL_TEXT_NAMES,  # noqa: F401
    _canonical_json as _canonical_json,  # noqa: F401
    _clip,
    _contains_source_secret_like as _contains_source_secret_like,  # noqa: F401
    _coverage_relevant_skip as _coverage_relevant_skip,  # noqa: F401
    _sha256_bytes as _sha256_bytes,  # noqa: F401
    _sha256_file as _sha256_file,  # noqa: F401
    _sha256_json as _sha256_json,  # noqa: F401
    _utc_now,
    repository_index_error_retryable,
)

_LOGGER = logging.getLogger(__name__)


def _index_report(
    *,
    state: RepositoryState,
    store: SQLiteRepositoryIndexStore,
    job: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None,
    started: float,
    processed_this_run: int,
    reused_this_run: int,
    resumed: bool,
    deduplicated: bool,
    pointer_repaired: bool = False,
    force_refresh: bool = False,
) -> dict[str, Any]:
    elapsed_ms = (time.perf_counter() - started) * 1_000
    progress = {
        "total_candidates": int(job["total_candidates"]),
        "processed_candidates": int(job["processed_count"]),
        "indexed_files": int(job["indexed_count"]),
        "skipped_files": int(job["skipped_count"]),
        "processed_this_run": processed_this_run,
        "reused_unchanged_files": reused_this_run,
        "reused_unchanged_files_total": int(job.get("reused_count", 0)),
        "cursor_path": job.get("cursor_path"),
    }
    complete = str(job["status"]) == "completed" and snapshot is not None
    snapshot_verified = False
    if snapshot is not None:
        snapshot_verified = verify_repository_snapshot(
            store.snapshot(str(snapshot["snapshot_id"]), include_files=True)
        )
    snapshot_report = _bounded_snapshot_report(snapshot) if snapshot is not None else None
    return {
        "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
        "ok": complete and snapshot_verified if complete else str(job["status"]) == "running",
        "status": str(job["status"]),
        "project": state.project,
        "root_id": state.root_id,
        "job_id": str(job["job_id"]),
        "snapshot_id": str(snapshot["snapshot_id"]) if snapshot else None,
        "state": state.summary(),
        "progress": progress,
        "snapshot": snapshot_report,
        "metrics": {
            "duration_ms": round(elapsed_ms, 3),
            "milliseconds_per_candidate": round(elapsed_ms / max(processed_this_run, 1), 3),
            "reused_unchanged_files": int(job.get("reused_count", 0)),
            "reused_unchanged_files_this_run": reused_this_run,
            "resumed": resumed,
            "deduplicated": deduplicated,
            "pointer_repaired": pointer_repaired,
            "force_refresh": bool(force_refresh),
            "sqlite_rows_written_estimate": processed_this_run + (4 if complete and not deduplicated else 0),
        },
        "gates": {
            "complete_snapshot_only": complete,
            "snapshot_checksum_valid": snapshot_verified if complete else None,
            "current_pointer_matches": bool(snapshot) and str(snapshot["snapshot_id"]) == str(job.get("snapshot_id")),
            "raw_source_persisted": False,
            "retrieval_published": False,
            "qdrant_written": False,
            "model_started": False,
        },
        "execution": {
            "writes_sqlite_state": not deduplicated,
            "writes_memory_rows": False,
            "writes_qdrant": False,
            "starts_background_daemon": False,
            "force_refresh": bool(force_refresh),
        },
    }


def _bounded_snapshot_report(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    payload = dict(snapshot)
    delta = dict(payload.get("delta") or {})
    for key in ("added", "changed", "removed", "renamed"):
        values = list(delta.get(key) or [])
        delta[f"{key}_count"] = len(values)
        delta[f"{key}_truncated"] = len(values) > REPOSITORY_INDEX_REPORT_LIST_LIMIT
        delta[key] = values[:REPOSITORY_INDEX_REPORT_LIST_LIMIT]
    payload["delta"] = delta
    return payload


def index_repository(
    root: str | Path,
    database_path: str | Path,
    *,
    project: str = "blackholememory",
    limits: RepositoryIndexLimits | None = None,
    source: RepositorySourceProvenance | None = None,
    max_files_per_run: int | None = None,
    fail_before_publish: bool = False,
    force_refresh: bool = False,
    expected_job_id: str | None = None,
    expected_state_digest: str | None = None,
    probe_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Build or resume one staged repository snapshot."""

    started = time.perf_counter()
    active_limits = limits or RepositoryIndexLimits()
    active_source = source or RepositorySourceProvenance()
    state = probe_repository_state(root, project=project, limits=active_limits, probe_timeout_seconds=probe_timeout_seconds)
    if expected_state_digest and str(state.state_digest) != str(expected_state_digest):
        raise RepositoryIndexError("repository state changed; restart the index slice")
    store = SQLiteRepositoryIndexStore(database_path)
    store.initialize()
    previous_snapshot = store.current_snapshot(state.project, state.root_id, include_files=True)
    previous_files = {
        str(item["path"]): dict(item)
        for item in (previous_snapshot.get("files", []) if previous_snapshot else [])
    }
    refresh_nonce = f"operator-refresh-{time.time_ns()}" if force_refresh else None
    job = store.begin_or_resume_job(state, active_source, force_refresh_nonce=refresh_nonce)
    if expected_job_id and str(job.get("job_id")) != str(expected_job_id):
        raise RepositoryIndexError("repository index continuation job changed; restart the index slice")
    if not force_refresh and job["status"] == "completed" and job.get("snapshot_id"):
        snapshot = store.snapshot(str(job["snapshot_id"]), include_files=False)
        current = store.current_snapshot(state.project, state.root_id, include_files=False)
        pointer_repaired = current is None or str(current.get("snapshot_id")) != str(snapshot.get("snapshot_id"))
        if pointer_repaired:
            store.promote_snapshot(
                str(snapshot["snapshot_id"]),
                project=state.project,
                root_id=state.root_id,
            )
        return _index_report(
            state=state,
            store=store,
            job=job,
            snapshot=snapshot,
            started=started,
            processed_this_run=0,
            reused_this_run=0,
            resumed=True,
            deduplicated=not pointer_repaired,
            pointer_repaired=pointer_repaired,
            force_refresh=False,
        )
    processed_before = int(job["processed_count"])
    processed = store.processed_paths(str(job["job_id"]))
    pending = [candidate for candidate in state.candidates if candidate.path not in processed]
    if max_files_per_run is not None and int(max_files_per_run) < 1:
        raise ValueError("max_files_per_run must be positive when provided")
    limit = len(pending) if max_files_per_run is None else min(len(pending), int(max_files_per_run))
    selected = pending[:limit]
    file_batch: list[dict[str, Any]] = []
    skip_batch: list[RepositorySkip] = []
    cursor: str | None = job.get("cursor_path")
    processed_this_run = 0
    reused_this_run = 0
    reused_batch = 0
    git_changed_paths = set(state.git_changed_paths)
    try:
        for candidate in selected:
            previous = previous_files.get(candidate.path)
            trusted_tracked_reuse = (
                candidate.origin == "tracked"
                and previous_snapshot is not None
                and previous_snapshot.get("git_head") == state.git_head
                and candidate.path not in git_changed_paths
            )
            can_reuse = (
                previous is not None
                and int(previous.get("size_bytes", -1)) == candidate.size_bytes
                and int(previous.get("mtime_ns", -1)) == candidate.mtime_ns
                # Untracked/filesystem candidates have no immutable revision
                # anchor.  Size and mtime are insufficient to prove content
                # identity (an attacker can replace bytes while preserving
                # both), so always re-read them and recompute the digest.
                and trusted_tracked_reuse
            )
            if can_reuse:
                indexed = {
                    "path": candidate.path,
                    "content_sha256": previous["content_sha256"],
                    "size_bytes": candidate.size_bytes,
                    "line_count": previous["line_count"],
                    "mtime_ns": candidate.mtime_ns,
                    "language": previous["language"],
                    "file_kind": previous["file_kind"],
                    "origin": candidate.origin,
                }
                skipped = None
                reused_this_run += 1
                reused_batch += 1
            else:
                indexed, skipped = _read_candidate(state.root, candidate)
            if indexed is not None:
                file_batch.append(indexed)
            if skipped is not None:
                skip_batch.append(skipped)
            cursor = candidate.path
            processed_this_run += 1
            if len(file_batch) + len(skip_batch) >= active_limits.batch_size:
                job = store.save_batch(
                    str(job["job_id"]),
                    file_batch,
                    skip_batch,
                    cursor_path=cursor,
                    reused_count_delta=reused_batch,
                )
                file_batch = []
                skip_batch = []
                reused_batch = 0
        if file_batch or skip_batch or (processed_this_run and int(job["processed_count"]) == processed_before):
            job = store.save_batch(
                str(job["job_id"]),
                file_batch,
                skip_batch,
                cursor_path=cursor,
                reused_count_delta=reused_batch,
            )
        if int(job["processed_count"]) < int(job["total_candidates"]):
            return _index_report(
                state=state,
                store=store,
                job=job,
                snapshot=None,
                started=started,
                processed_this_run=processed_this_run,
                reused_this_run=reused_this_run,
                resumed=processed_before > 0,
                deduplicated=False,
                force_refresh=force_refresh,
            )
        final_state = probe_repository_state(state.root, project=project, limits=active_limits)
        if final_state.state_digest != state.state_digest:
            raise RepositoryStateChangedError("repository state changed before snapshot publication")
        if fail_before_publish:
            raise RepositoryIndexInjectedFailure("injected failure before current-snapshot publication")
        snapshot = store.finalize_job(str(job["job_id"]))
        job = store.job(str(job["job_id"]))
        return _index_report(
            state=state,
            store=store,
            job=job,
            snapshot=snapshot,
            started=started,
            processed_this_run=processed_this_run,
            reused_this_run=reused_this_run,
            resumed=processed_before > 0,
            deduplicated=False,
            force_refresh=force_refresh,
        )
    except Exception as exc:  # broad: mark every failed indexing job before re-raising
        _LOGGER.error("repository indexing failed; marking job failed")
        store.mark_failed(
            str(job["job_id"]),
            code=type(exc).__name__,
            detail=str(exc),
        )
        raise


class RepositoryWatcher:
    """Bounded polling watcher; it never starts itself or escapes the root."""

    def __init__(
        self,
        root: str | Path,
        database_path: str | Path,
        *,
        project: str = "blackholememory",
        limits: RepositoryIndexLimits | None = None,
        source: RepositorySourceProvenance | None = None,
        max_inflight_jobs: int = DEFAULT_WATCH_MAX_INFLIGHT_JOBS,
        retry_attempts: int = DEFAULT_WATCH_RETRY_ATTEMPTS,
        retry_backoff_seconds: float = DEFAULT_WATCH_RETRY_BACKOFF_SECONDS,
    ) -> None:
        # lgtm [py/path-injection]
        self.root = assert_safe_path(
            Path(root).expanduser(),
            reject_hardlink_target=False,
        )
        lexical_database = Path(database_path).expanduser()
        assert_safe_path(lexical_database)
        self.database_path = lexical_database.resolve()
        assert_safe_path(self.database_path)
        self.project = _clip(project, 120).strip() or "blackholememory"
        self.limits = limits or RepositoryIndexLimits()
        self.source = source or RepositorySourceProvenance()
        if not 1 <= int(max_inflight_jobs) <= MAX_WATCH_MAX_INFLIGHT_JOBS:
            raise ValueError(
                f"max_inflight_jobs must be between 1 and {MAX_WATCH_MAX_INFLIGHT_JOBS}"
            )
        self.max_inflight_jobs = int(max_inflight_jobs)
        if not 1 <= int(retry_attempts) <= 4:
            raise ValueError("retry_attempts must be between 1 and 4")
        if not 0 <= float(retry_backoff_seconds) <= 30:
            raise ValueError("retry_backoff_seconds must be between 0 and 30")
        self.retry_attempts = int(retry_attempts)
        self.retry_backoff_seconds = float(retry_backoff_seconds)

    def backpressure(self) -> dict[str, Any]:
        """Return bounded operator backpressure state without writing state."""

        state = probe_repository_state(self.root, project=self.project, limits=self.limits)
        return self._backpressure_for_state(state.root_id, state.state_digest)

    def _backpressure_for_state(self, root_id: str, state_digest: str) -> dict[str, Any]:
        store = SQLiteRepositoryIndexStore(self.database_path)
        running = store.running_jobs(self.project, root_id, limit=MAX_WATCH_MAX_INFLIGHT_JOBS + 1)
        blockers = [job for job in running if str(job.get("state_digest")) != str(state_digest)]
        # A stale job for the exact same state is resumable by index_repository;
        # a job for a different state consumes the operator's bounded capacity.
        blocked = len(running) >= self.max_inflight_jobs and bool(blockers)
        return {
            "schema_version": "bhm.repository-watch-backpressure.v1",
            "project": self.project,
            "root_id": root_id,
            "state_digest": state_digest,
            "max_inflight_jobs": self.max_inflight_jobs,
            "retry_attempts": self.retry_attempts,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "active_job_count": len(running),
            "active_jobs": [
                {
                    "job_id": str(job.get("job_id")),
                    "state_digest": str(job.get("state_digest")),
                    "processed_count": int(job.get("processed_count") or 0),
                    "total_candidates": int(job.get("total_candidates") or 0),
                    "updated_at": str(job.get("updated_at")),
                }
                for job in running
            ],
            "blocking_job_count": len(blockers),
            "blocked": blocked,
            "operator_managed": True,
            "autonomous_apply": False,
            "starts_background_daemon": False,
            "writes_sqlite_state": False,
            "writes_qdrant": False,
            "raw_source_returned": False,
        }

    def poll(self) -> dict[str, Any]:
        state = probe_repository_state(self.root, project=self.project, limits=self.limits)
        current = SQLiteRepositoryIndexStore(self.database_path).current_snapshot(
            self.project,
            state.root_id,
            include_files=False,
        )
        changed = current is None or str(current["state_digest"]) != state.state_digest
        return {
            "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
            "changed": changed,
            "state": state.summary(),
            "current_snapshot_id": str(current["snapshot_id"]) if current else None,
            "current_state_digest": str(current["state_digest"]) if current else None,
            "starts_background_daemon": False,
            "writes_sqlite_state": False,
        }

    def run(
        self,
        *,
        cycles: int = 1,
        interval_seconds: float = DEFAULT_WATCH_INTERVAL_SECONDS,
        debounce_seconds: float = 0.0,
        index_on_change: bool = True,
    ) -> dict[str, Any]:
        if not 1 <= int(cycles) <= 100:
            raise ValueError("cycles must be between 1 and 100")
        if not 0 <= float(interval_seconds) <= 300:
            raise ValueError("interval_seconds must be between 0 and 300")
        if not 0 <= float(debounce_seconds) <= 30:
            raise ValueError("debounce_seconds must be between 0 and 30")
        events: list[dict[str, Any]] = []
        store = SQLiteRepositoryIndexStore(self.database_path)
        initial_state = probe_repository_state(self.root, project=self.project, limits=self.limits)
        previous_checkpoint = store.get_watch_checkpoint(self.project, initial_state.root_id)
        resumed_from_checkpoint = bool(previous_checkpoint and previous_checkpoint.get("status") == "running")
        for cycle in range(int(cycles)):
            poll = self.poll()
            backpressure = self._backpressure_for_state(
                str(poll["state"]["root_id"]),
                str(poll["state"].get("state_digest")),
            )
            event: dict[str, Any] = {
                "cycle": cycle + 1,
                "poll": poll,
                "backpressure": backpressure,
                "index": None,
            }
            checkpoint = store.save_watch_checkpoint(
                self.project,
                str(poll["state"]["root_id"]),
                {
                    "status": "running",
                    "cycle": cycle + 1,
                    "cycles": int(cycles),
                    "state_digest": poll["state"].get("state_digest"),
                    "current_snapshot_id": poll.get("current_snapshot_id"),
                    "updated_at": _utc_now(),
                    "resume_source": "crash-recovery" if resumed_from_checkpoint else "new-run",
                },
            )
            event["checkpoint"] = checkpoint
            if poll["changed"] and index_on_change:
                if backpressure["blocked"]:
                    event["backpressured"] = True
                    event["requires_operator_action"] = True
                elif debounce_seconds:
                    time.sleep(float(debounce_seconds))
                    stable_poll = self.poll()
                    event["debounce"] = {"seconds": float(debounce_seconds), "stable": bool(stable_poll["changed"]), "poll": stable_poll}
                    if not stable_poll["changed"]:
                        event["debounced"] = True
                if not event.get("debounced") and not backpressure["blocked"]:
                    index_error: BaseException | None = None
                    for attempt in range(1, self.retry_attempts + 1):
                        try:
                            event["index"] = index_repository(
                                self.root,
                                self.database_path,
                                project=self.project,
                                limits=self.limits,
                                source=self.source,
                            )
                            event["retry"] = {
                                "attempts": attempt,
                                "max_attempts": self.retry_attempts,
                                "recovered": attempt > 1,
                            }
                            break
                        except Exception as exc:  # broad: watcher must classify and persist any job failure
                            _LOGGER.error("repository watcher indexing attempt failed")
                            index_error = exc
                            retryable = repository_index_error_retryable(exc)
                            if not retryable or attempt >= self.retry_attempts:
                                failed_checkpoint = store.save_watch_checkpoint(
                                    self.project,
                                    str(poll["state"]["root_id"]),
                                    {
                                        "status": "failed",
                                        "cycle": cycle + 1,
                                        "cycles": int(cycles),
                                        "state_digest": poll["state"].get("state_digest"),
                                        "current_snapshot_id": poll.get("current_snapshot_id"),
                                        "updated_at": _utc_now(),
                                        "resume_source": "crash-recovery" if resumed_from_checkpoint else "new-run",
                                        "retryable": bool(retryable),
                                        "error_code": type(exc).__name__,
                                        "error": _clip(str(exc), 500),
                                        "attempts": attempt,
                                        "max_attempts": self.retry_attempts,
                                        "next_retry_at": _utc_now() if retryable else None,
                                    },
                                )
                                event["checkpoint"] = failed_checkpoint
                                event["error"] = {
                                    "code": type(exc).__name__,
                                    "detail": _clip(str(exc), 500),
                                    "retryable": bool(retryable),
                                }
                                event["retry"] = {
                                    "attempts": attempt,
                                    "max_attempts": self.retry_attempts,
                                    "recovered": False,
                                }
                                if retryable:
                                    break
                                raise
                            delay = self.retry_backoff_seconds * (2 ** (attempt - 1))
                            if delay:
                                time.sleep(delay)
                    if index_error is not None and event.get("index") is None and "error" not in event:
                        raise index_error
            if event.get("error"):
                events.append(event)
                if cycle + 1 < int(cycles) and interval_seconds:
                    time.sleep(float(interval_seconds))
                continue
            store.save_watch_checkpoint(
                self.project,
                str(poll["state"]["root_id"]),
                {
                    "status": "completed",
                    "cycle": cycle + 1,
                    "cycles": int(cycles),
                    "state_digest": poll["state"].get("state_digest"),
                    "snapshot_id": (event.get("index") or {}).get("snapshot_id") or poll.get("current_snapshot_id"),
                    "updated_at": _utc_now(),
                    "resume_source": "crash-recovery" if resumed_from_checkpoint else "new-run",
                    "result": "backpressured" if event.get("backpressured") else "completed",
                },
            )
            events.append(event)
            if cycle + 1 < int(cycles) and interval_seconds:
                time.sleep(float(interval_seconds))
        return {
            "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
            "ok": all(
                not event.get("error")
                and (event["index"] is None or bool(event["index"]["ok"]))
                for event in events
            ),
            "cycles": int(cycles),
            "debounce_seconds": float(debounce_seconds),
            "max_inflight_jobs": self.max_inflight_jobs,
            "retry_attempts": self.retry_attempts,
            "retry_backoff_seconds": self.retry_backoff_seconds,
            "backpressured_cycles": sum(1 for event in events if event.get("backpressured")),
            "operator_managed": True,
            "autonomous_apply": False,
            "resumed_from_checkpoint": resumed_from_checkpoint,
            "events": events,
            "starts_background_daemon": False,
        }


def repository_index_status(
    root: str | Path,
    database_path: str | Path,
    *,
    project: str = "blackholememory",
    limits: RepositoryIndexLimits | None = None,
    probe_timeout_seconds: float | None = None,
    fast_schema: bool = False,
) -> dict[str, Any]:
    state = probe_repository_state(root, project=project, limits=limits, probe_timeout_seconds=probe_timeout_seconds)
    store = SQLiteRepositoryIndexStore(database_path)
    if not store.path.exists():
        return {
            "schema_version": REPOSITORY_INDEX_SCHEMA_VERSION,
            "database_path": str(store.path),
            "project": state.project,
            "root_id": state.root_id,
            "current_snapshot": None,
            "latest_job": None,
            "current_state": state.summary(),
            "fresh": False,
            "freshness_status": "missing",
            "retryable": False,
        }
    status = store.status(state.project, state.root_id, fast_schema=fast_schema)
    current = status.get("current_snapshot")
    status["current_state"] = state.summary()
    status["fresh"] = bool(current) and str(current["state_digest"]) == state.state_digest
    status["freshness_status"] = "fresh" if status["fresh"] else "stale"
    status["retryable"] = bool(status.get("retryable", False))
    return status


__all__ = [
    "REPOSITORY_INDEX_SCHEMA_VERSION",
    "RepositoryCandidate",
    "RepositoryIndexError",
    "RepositoryIndexInjectedFailure",
    "RepositoryIndexLimits",
    "RepositoryIndexMigrationRequired",
    "RepositoryRootError",
    "RepositorySkip",
    "RepositorySourceProvenance",
    "RepositoryState",
    "RepositoryStateChangedError",
    "RepositoryWatcher",
    "SQLiteRepositoryIndexStore",
    "index_repository",
    "repository_index_error_retryable",
    "probe_repository_state",
    "repository_root_id",
    "repository_index_status",
    "verify_repository_snapshot",
]
