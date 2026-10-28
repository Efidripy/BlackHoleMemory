"""Operator actions of the launcher (FIX-017 follow-up): bounded operator JSON/report runners and the operator workflows (integrity audit, backup/restore, cleanup, projection, consolidation, reconcile, exchange)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import importlib.util
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
import json
import os
import shutil
import sqlite3
import subprocess

from bhm_launcher_readiness import MAX_HTTP_BYTES

from bhm_launcher_host import launcher as _launcher


def _operator_database_path() -> Path:
    return _launcher.find_project_root() / ".runtime" / "live-memory" / "memories.sqlite3"


def _operator_runtime_root(name: str) -> Path:
    root = _launcher.find_project_root() / ".runtime" / name
    root.mkdir(parents=True, exist_ok=True)
    return root


def _resolve_owned_runtime_file(value: str, root: Path, *, must_exist: bool = True) -> Path:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("file path is required")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    _launcher._assert_owned_path(resolved, root, require_directory=False)
    if must_exist and not resolved.is_file():
        raise FileNotFoundError(resolved)
    return resolved


def _run_operator_json(args: list[str], *, cwd: Path, timeout: float = _launcher.PROCESS_CONTROL_TIMEOUT_SECONDS) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        creationflags=_launcher.creation_flags(),
        startupinfo=_launcher.hidden_startupinfo(),
        check=False,
    )
    output = (completed.stdout or "").strip()
    if not output:
        raise RuntimeError(_launcher.compact_error(RuntimeError(completed.stderr.strip() or "operator returned no output")))
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"operator returned invalid JSON: {_launcher.compact_error(exc)}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("operator response must be an object")
    if completed.returncode != 0 or payload.get("ok") is False:
        raise RuntimeError(str(payload.get("error") or payload.get("detail") or "operator action failed")[:240])
    return payload


def _operator_receipt_path(folder: str, prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = (_launcher._operator_runtime_root("operator-tools") / folder / f"{prefix}-{stamp}.json").resolve()
    _launcher._assert_owned_path(target, _launcher.find_project_root() / ".runtime", require_directory=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _operator_projection_report_path(prefix: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report_root = (_launcher.find_project_root() / ".runtime" / "reports").resolve()
    target = (report_root / "operator-tools" / f"{prefix}-{stamp}.json").resolve()
    _launcher._assert_owned_path(target, report_root, require_directory=False)
    target.parent.mkdir(parents=True, exist_ok=True)
    return target


def _run_operator_report(
    args: list[str],
    *,
    cwd: Path,
    report: Path,
    timeout: float = _launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    completed = subprocess.run(
        args,
        cwd=str(cwd),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=timeout,
        creationflags=_launcher.creation_flags(),
        startupinfo=_launcher.hidden_startupinfo(),
        check=False,
    )
    if not report.is_file():
        raise RuntimeError("operator did not produce its bounded report")
    payload = json.loads(report.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("operator report must be an object")
    if completed.returncode != 0 or payload.get("ok") is False or payload.get("success") is False:
        raise RuntimeError(str(payload.get("error") or payload.get("detail") or "operator action failed")[:240])
    return {"report_path": str(report), **payload}


def operator_integrity_audit(project: str) -> dict[str, Any]:
    result = _launcher.safe_json_request(
        f"{_launcher.BHM_BASE_URL}/bhm/integrity-audit?{urlencode({'project': project})}",
        method="POST",
        payload={},
        project=project,
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
        max_bytes=min(MAX_HTTP_BYTES * 8, 1024 * 1024),
    )
    if not result.ok:
        raise RuntimeError(result.error or f"HTTP {result.status_code}")
    return {"action": "integrity", "project": project, **result.data}


def operator_cleanup_preview(project: str) -> dict[str, Any]:
    python = str(_launcher.venv_python(_launcher.find_project_root()) if _launcher.has_virtualenv(_launcher.find_project_root()) else _launcher.host_python_executable())
    cli = _launcher.find_project_root() / "scripts" / "bhm-sqlite-retention.py"
    _launcher._assert_launchable_source(cli, _launcher.find_project_root())
    report = _launcher._run_operator_json(
        [python, str(cli), "--database", str(_launcher._operator_database_path())],
        cwd=_launcher.find_project_root(),
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
    )
    return {"action": "cleanup", "project": project, "phase": "preview", **report}


def _requeue_script_module() -> Any:
    script = _launcher.find_project_root() / "scripts" / "requeue-bhm-dead-letters.py"
    _launcher._assert_launchable_source(script, _launcher.find_project_root())
    spec = importlib.util.spec_from_file_location("requeue_bhm_dead_letters", script)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load dead-letter requeue operator")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _requeue_last_error_counts(database: Path) -> list[dict[str, Any]]:
    connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT COALESCE(last_error, ''), COUNT(*)
            FROM memory_outbox
            WHERE status = 'dead_letter'
            GROUP BY COALESCE(last_error, '')
            ORDER BY COALESCE(last_error, '')
            """
        ).fetchall()
    finally:
        connection.close()
    return [{"last_error": str(error), "count": int(count)} for error, count in rows]


def operator_requeue_preview(project: str) -> dict[str, Any]:
    database = _launcher._operator_database_path().resolve()
    module = _requeue_script_module()
    event_ids, error = module._read_dead_letters(database)
    if error:
        raise RuntimeError(f"cannot preview dead-letter requeue: {error}")
    return {
        "action": "requeue",
        "project": project,
        "phase": "preview",
        "mode": "ro",
        "writes_live_state": False,
        "dead_letter_count": len(event_ids),
        "event_digest": module._event_digest(event_ids),
        "last_errors": _requeue_last_error_counts(database),
    }


def operator_requeue_apply(preview: dict[str, Any], project: str) -> dict[str, Any]:
    digest = str(preview.get("event_digest") or "")
    if not digest:
        raise RuntimeError("requeue preview did not provide an event digest")
    root = _launcher.find_project_root()
    database = _launcher._operator_database_path().resolve()
    module = _requeue_script_module()
    current_ids, error = module._read_dead_letters(database)
    if error:
        raise RuntimeError(f"cannot revalidate dead-letter preview: {error}")
    current_digest = module._event_digest(current_ids)
    if current_digest != digest:
        raise RuntimeError("dead-letter preview is stale; re-run the read-only preview")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = (_launcher._operator_runtime_root("backups") / "requeue" / stamp).resolve()
    _launcher._assert_owned_path(backup_root, root / ".runtime", require_directory=False)
    manifest = module._apply(database, backup_root, digest)
    return {
        "action": "requeue",
        "project": project,
        "phase": "apply",
        "writes_live_state": True,
        "event_digest": digest,
        "dead_letter_count": len(current_ids),
        "backup": manifest.get("backup"),
        "manifest": manifest,
    }


def operator_sqlite_backup(_project: str) -> dict[str, Any]:
    from blackholememory.sqlite_retention import create_verified_sqlite_backup

    database = _launcher._operator_database_path().resolve()
    if not database.is_file():
        raise FileNotFoundError(database)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = (_launcher._operator_runtime_root("backups") / "sqlite-retention" / stamp / "memories.sqlite3").resolve()
    _launcher._assert_owned_path(target, _launcher.find_project_root() / ".runtime", require_directory=False)
    verification = create_verified_sqlite_backup(database, target)
    return {"action": "backup", "path": str(target), "verification": verification}


def operator_restore_backup(path: str, _project: str) -> dict[str, Any]:
    from blackholememory.sqlite_retention import create_verified_sqlite_backup, verify_sqlite_database

    root = _launcher.find_project_root()
    backup_root = (root / ".runtime" / "backups").resolve()
    source = _launcher._resolve_owned_runtime_file(path, backup_root)
    source_check = verify_sqlite_database(source)
    if not source_check.get("ok"):
        raise RuntimeError("selected backup failed SQLite verification")
    database = _launcher._operator_database_path().resolve()
    pre_restore = (
        backup_root
        / "pre-restore"
        / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        / "memories.sqlite3"
    ).resolve()
    stopped, detail = _launcher.run_canonical_api_command(root, stop_only=True)
    if not stopped:
        raise RuntimeError(f"API stop failed: {detail}")
    try:
        if database.exists():
            create_verified_sqlite_backup(database, pre_restore)
        staging = database.with_suffix(".restore-staging.sqlite3")
        _launcher._assert_owned_path(staging, root / ".runtime", require_directory=False)
        shutil.copy2(source, staging)
        if not verify_sqlite_database(staging).get("ok"):
            raise RuntimeError("staged restore failed SQLite verification")
        os.replace(staging, database)
        after = verify_sqlite_database(database)
    except Exception:
        if pre_restore.exists():
            shutil.copy2(pre_restore, database)
        raise
    finally:
        restarted, restart_detail = _launcher.run_canonical_api_command(root)
        if not restarted:
            raise RuntimeError(f"API restart failed after SQLite restore: {restart_detail}")
    return {"action": "restore", "source": str(source), "pre_restore_backup": str(pre_restore), "verification": after}


def operator_restore_preview(path: str) -> dict[str, Any]:
    from blackholememory.sqlite_retention import verify_sqlite_database

    backup_root = (_launcher.find_project_root() / ".runtime" / "backups").resolve()
    source = _launcher._resolve_owned_runtime_file(path, backup_root)
    verification = verify_sqlite_database(source)
    if not verification.get("ok"):
        raise RuntimeError("selected backup failed SQLite verification")
    return {"action": "restore", "phase": "preview", "source": str(source), "verification": verification}


def operator_cleanup_apply(preview: dict[str, Any], project: str) -> dict[str, Any]:
    retention = preview.get("retention") or preview.get("plan") or {}
    digest = str(retention.get("plan_digest") or retention.get("planDigest") or "")
    as_of = str(retention.get("as_of") or retention.get("asOf") or preview.get("as_of") or "")
    if not digest or not as_of:
        raise RuntimeError("cleanup preview did not provide a plan digest and timestamp")
    root = _launcher.find_project_root()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = (_launcher._operator_runtime_root("backups") / "sqlite-retention" / stamp / "memories.sqlite3").resolve()
    _launcher._assert_owned_path(backup_path, root / ".runtime", require_directory=False)
    python = str(_launcher.venv_python(root) if _launcher.has_virtualenv(root) else _launcher.host_python_executable())
    cli = root / "scripts" / "bhm-sqlite-retention.py"
    stopped, detail = _launcher.run_canonical_api_command(root, stop_only=True)
    if not stopped:
        raise RuntimeError(f"API stop failed: {detail}")
    try:
        applied = _launcher._run_operator_json(
            [python, str(cli), "--apply", "--offline", "--as-of", as_of, "--confirm-plan-digest", digest, "--backup", str(backup_path)],
            cwd=root,
            timeout=_launcher.API_START_COMMAND_TIMEOUT_SECONDS,
        )
    finally:
        restarted, restart_detail = _launcher.run_canonical_api_command(root)
        if not restarted:
            raise RuntimeError(f"API restart failed after SQLite cleanup: {restart_detail}")
    return {"action": "cleanup", "phase": "apply", "backup": str(backup_path), "result": applied}


def operator_repair_indexes(project: str) -> dict[str, Any]:
    backup = _launcher.operator_sqlite_backup(project)
    result = _launcher.safe_json_request(
        f"{_launcher.BHM_BASE_URL}/bhm/integrity/repair-strict",
        method="POST",
        payload={"project": project, "aggregate": False, "remove_orphan_links": True, "remove_orphan_artifacts": False, "normalize_metadata": True},
        project=project,
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
        admin=True,
    )
    if not result.ok:
        raise RuntimeError(result.error or f"HTTP {result.status_code}")
    return {"action": "repair", "project": project, "backup": backup, **result.data}


def operator_projection_rebuild(project: str) -> dict[str, Any]:
    root = _launcher.find_project_root()
    script = root / "scripts" / "bhm-projection-operator.ps1"
    _launcher._assert_launchable_source(script, root)
    backup = _launcher.operator_sqlite_backup(project)
    result = _launcher._run_operator_json(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", "drain", "-MaxCycles", "32", "-AsJson"],
        cwd=root,
        timeout=_launcher.API_START_COMMAND_TIMEOUT_SECONDS,
    )
    return {"action": "projection", "backup": backup, "result": result}


def operator_projection_status(_project: str) -> dict[str, Any]:
    root = _launcher.find_project_root()
    script = root / "scripts" / "bhm-projection-operator.ps1"
    _launcher._assert_launchable_source(script, root)
    result = _launcher._run_operator_json(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script), "-Action", "status", "-AsJson"],
        cwd=root,
    )
    return {"action": "projection_status", **result}


def operator_qdrant_catalog(project: str) -> dict[str, Any]:
    result = _launcher.safe_json_request(
        f"{_launcher.BHM_BASE_URL}/bhm/telemetry/qdrant-catalog",
        method="GET",
        project=project,
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
        max_bytes=min(MAX_HTTP_BYTES * 8, 1024 * 1024),
    )
    if not result.ok:
        raise RuntimeError(result.error or f"HTTP {result.status_code}")
    return {"action": "qdrant_catalog", **result.data}


def operator_orphan_classification(project: str) -> dict[str, Any]:
    root = _launcher.find_project_root()
    script = root / "scripts" / "bhm_classify_projection_orphans.py"
    _launcher._assert_launchable_source(script, root)
    python = str(_launcher.venv_python(root) if _launcher.has_virtualenv(root) else _launcher.host_python_executable())
    report = _launcher._operator_projection_report_path("orphan-classification")
    result = _launcher._run_operator_json(
        [python, str(script), "--database", str(_launcher._operator_database_path()), "--project", project, "--report", str(report), "--summary-only"],
        cwd=root,
        timeout=_launcher.API_START_COMMAND_TIMEOUT_SECONDS,
    )
    return {"action": "orphan_classification", "report_path": str(report), **result}


def operator_index_status(project: str) -> dict[str, Any]:
    root = _launcher.find_project_root()
    script = root / "scripts" / "bhm-repository-index.py"
    _launcher._assert_launchable_source(script, root)
    python = str(_launcher.venv_python(root) if _launcher.has_virtualenv(root) else _launcher.host_python_executable())
    status_report = _launcher._operator_receipt_path("repository-index", "status")
    plan_report = _launcher._operator_receipt_path("repository-index", "plan")
    common = ["--root", str(root), "--database", str(_launcher._operator_database_path()), "--project", project]
    status = _launcher._run_operator_report(
        [python, str(script), "--action", "status", *common, "--report", str(status_report)],
        cwd=root,
        report=status_report,
    )
    plan = _launcher._run_operator_report(
        [python, str(script), "--action", "plan", *common, "--report", str(plan_report)],
        cwd=root,
        report=plan_report,
    )
    return {"action": "index_status", "project": project, "status": status, "plan": plan}


def operator_receipts(_project: str) -> dict[str, Any]:
    runtime = (_launcher.find_project_root() / ".runtime").resolve()
    roots = [
        runtime / name
        for name in ("operator-tools", "reports", "retention", "data-hygiene", "backups", "admin-exports")
    ]
    items: list[dict[str, Any]] = []
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.casefold() not in {".json", ".zip", ".sqlite3"}:
                continue
            try:
                metadata = path.stat()
                relative = path.resolve().relative_to(runtime)
            except (OSError, ValueError):
                continue
            items.append(
                {
                    "path": str(relative),
                    "size_bytes": int(metadata.st_size),
                    "modified_at": datetime.fromtimestamp(metadata.st_mtime).isoformat(timespec="seconds"),
                }
            )
    items.sort(key=lambda item: str(item["modified_at"]), reverse=True)
    return {"action": "receipts", "runtime_root": ".runtime", "count": len(items), "items": items[:100]}


def operator_consolidation_preview(project: str) -> dict[str, Any]:
    """Read the bounded governed queue; no proposal or memory mutation occurs."""

    result = _launcher.safe_json_request(
        f"{_launcher.BHM_BASE_URL}/bhm/governed-consolidation/proposals?{urlencode({'project': project, 'status': 'proposed', 'limit': 200})}",
        method="GET",
        project=project,
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
        max_bytes=min(MAX_HTTP_BYTES * 8, 2 * 1024 * 1024),
        admin=True,
    )
    if not result.ok:
        raise RuntimeError(result.error or f"HTTP {result.status_code}")
    proposals = result.data.get("proposals") if isinstance(result.data.get("proposals"), list) else []
    return {
        "action": "consolidation",
        "phase": "preview",
        "project": project,
        "count": len(proposals),
        "proposals": proposals,
        "side_effects": {"read_only": True, "sqlite_mutation": False, "qdrant_mutation": False, "mem0_mutation": False},
    }


def operator_consolidation_apply(preview: dict[str, Any], project: str) -> dict[str, Any]:
    """Apply only eligible proposed items after one explicit operator consent."""

    from blackholememory.governed_auto_review import review_proposal

    proposals = preview.get("proposals") if isinstance(preview.get("proposals"), list) else []
    backup = _launcher.operator_sqlite_backup(project)
    applied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for raw in proposals[:200]:
        if not isinstance(raw, dict):
            continue
        proposal_id = str(raw.get("proposal_id") or "")
        if not proposal_id:
            continue
        decision = review_proposal(raw, allow_operator_consent=True)
        if decision.decision != "approve":
            skipped.append({"proposal_id": proposal_id, "reason_codes": list(decision.reason_codes)})
            continue
        approved = _launcher.safe_json_request(
            f"{_launcher.BHM_BASE_URL}/bhm/governed-consolidation/proposals/decision",
            method="POST",
            payload={"project": project, "proposal_id": proposal_id, "decision": "approve"},
            project=project,
            timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
            admin=True,
        )
        if not approved.ok:
            skipped.append({"proposal_id": proposal_id, "reason_codes": [approved.error or f"HTTP {approved.status_code}"]})
            continue
        result = _launcher.safe_json_request(
            f"{_launcher.BHM_BASE_URL}/bhm/governed-consolidation/proposals/apply",
            method="POST",
            payload={"project": project, "proposal_id": proposal_id, "apply": True, "confirmation": proposal_id},
            project=project,
            timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
            admin=True,
        )
        if result.ok:
            applied.append({"proposal_id": proposal_id, **result.data})
        else:
            skipped.append({"proposal_id": proposal_id, "reason_codes": [result.error or f"HTTP {result.status_code}"]})
    return {
        "action": "consolidation",
        "phase": "apply",
        "project": project,
        "backup": backup,
        "applied_count": len(applied),
        "skipped_count": len(skipped),
        "applied": applied,
        "skipped": skipped,
    }


def operator_reconcile_preview(project: str) -> dict[str, Any]:
    root = _launcher.find_project_root()
    script = root / "scripts" / "bhm_reconcile_projection.py"
    _launcher._assert_launchable_source(script, root)
    python = str(_launcher.venv_python(root) if _launcher.has_virtualenv(root) else _launcher.host_python_executable())
    report = _launcher._operator_projection_report_path("reconcile-preview")
    result = _launcher._run_operator_json(
        [python, str(script), "--database", str(_launcher._operator_database_path()), "--project", project, "--report", str(report), "--summary-only"],
        cwd=root,
        timeout=_launcher.API_START_COMMAND_TIMEOUT_SECONDS,
    )
    control_plane = result.get("controlPlane") if isinstance(result.get("controlPlane"), dict) else {}
    if control_plane.get("rebuildEligibility") != "eligible":
        raise RuntimeError(
            "projection reconciliation is blocked by its model/dimension compatibility receipt; "
            "no backup, stop or apply was started"
        )
    return {"action": "reconcile", "phase": "preview", "report_path": str(report), **result}


def operator_reconcile_apply(preview: dict[str, Any], project: str) -> dict[str, Any]:
    plan = preview.get("plan") if isinstance(preview.get("plan"), dict) else {}
    digest = str(preview.get("planDigest") or plan.get("digest") or plan.get("planDigest") or "")
    as_of = str(preview.get("asOf") or preview.get("as_of") or "")
    if not digest or not as_of:
        raise RuntimeError("projection preview did not return an exact digest and timestamp")
    root = _launcher.find_project_root()
    script = root / "scripts" / "bhm_reconcile_projection.py"
    _launcher._assert_launchable_source(script, root)
    python = str(_launcher.venv_python(root) if _launcher.has_virtualenv(root) else _launcher.host_python_executable())
    report = _launcher._operator_projection_report_path("reconcile-apply")
    backup = _launcher.operator_sqlite_backup(project)
    stopped, detail = _launcher.run_canonical_api_command(root, stop_only=True)
    if not stopped:
        raise RuntimeError(f"API stop failed: {detail}")
    try:
        result = _launcher._run_operator_json(
            [python, str(script), "--database", str(_launcher._operator_database_path()), "--project", project, "--as-of", as_of, "--apply", "--confirm-plan-digest", digest, "--report", str(report), "--summary-only"],
            cwd=root,
            timeout=_launcher.API_START_COMMAND_TIMEOUT_SECONDS,
        )
    finally:
        restarted, restart_detail = _launcher.run_canonical_api_command(root)
        if not restarted:
            raise RuntimeError(f"API restart failed after projection reconcile: {restart_detail}")
    return {"action": "reconcile", "phase": "apply", "backup": backup, "report_path": str(report), "result": result}


def operator_exchange_preview(path: str, project: str) -> dict[str, Any]:
    root = (_launcher.find_project_root() / ".runtime" / "admin-exports").resolve()
    snapshot = _launcher._resolve_owned_runtime_file(path, root)
    result = _launcher.safe_json_request(
        f"{_launcher.BHM_BASE_URL}/bhm/admin/import-preview",
        method="POST",
        payload={"path": str(snapshot), "project": project},
        project=project,
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
        admin=True,
    )
    if not result.ok:
        raise RuntimeError(result.error or f"HTTP {result.status_code}")
    return {"action": "exchange", "phase": "preview", **result.data}


def operator_export(project: str) -> dict[str, Any]:
    result = _launcher.safe_json_request(
        f"{_launcher.BHM_BASE_URL}/bhm/admin/export",
        method="POST",
        payload={"project": project, "include_archived": True, "include_artifacts": True},
        project=project,
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
        admin=True,
    )
    if not result.ok:
        raise RuntimeError(result.error or f"HTTP {result.status_code}")
    return {"action": "export", **result.data}


def operator_exchange_apply(path: str, project: str) -> dict[str, Any]:
    root = (_launcher.find_project_root() / ".runtime" / "admin-exports").resolve()
    snapshot = _launcher._resolve_owned_runtime_file(path, root)
    backup = _launcher.operator_sqlite_backup(project)
    result = _launcher.safe_json_request(
        f"{_launcher.BHM_BASE_URL}/bhm/admin/import-apply",
        method="POST",
        payload={"path": str(snapshot), "merge_mode": "upsert", "project": project},
        project=project,
        timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
        admin=True,
    )
    if not result.ok:
        raise RuntimeError(result.error or f"HTTP {result.status_code}")
    return {"action": "exchange", "phase": "apply", "backup": backup, **result.data}
