#!/usr/bin/env python
# ruff: noqa: E402
"""Verify a saved bhm_crystallize_worker test run."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.runtime_endpoints import endpoint_url


def _read_process_or_user_env_value(key: str) -> str | None:
    direct = str(os.getenv(key) or "").strip()
    if direct:
        return direct
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, key)
    except (ImportError, FileNotFoundError, OSError):
        return None
    return str(value or "").strip() or None


def _required_bhm_caller_token() -> str:
    token = _read_process_or_user_env_value("BHM_CALLER_TOKEN") or ""
    if len(token) < 32:
        raise RuntimeError("BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN")
    return token


def _bhm_client() -> httpx.Client:
    return httpx.Client(
        timeout=60.0,
        headers={"Authorization": f"Bearer {_required_bhm_caller_token()}"},
    )


def extract_worker_stdout(worker_log: Path) -> dict[str, Any]:
    text = read_text_flexible(worker_log)
    start_marker = "=== stdout ==="
    end_marker = "=== stderr ==="
    if start_marker not in text or end_marker not in text:
        raise RuntimeError("worker.log does not contain stdout/stderr markers")
    raw = text.split(start_marker, 1)[1].split(end_marker, 1)[0].strip()
    if not raw:
        raise RuntimeError("worker stdout section is empty")
    return json.loads(raw)


def read_text_flexible(path: Path) -> str:
    last_error: Exception | None = None
    for encoding in ("utf-8", "utf-8-sig", "utf-16"):
        try:
            return path.read_text(encoding=encoding)
        except UnicodeDecodeError as exc:
            last_error = exc
    raise RuntimeError(f"cannot decode {path}: {last_error}") from last_error


def get_memory(client: httpx.Client, base_url: str, memory_id: str, project: str) -> dict[str, Any]:
    response = client.get(f"{base_url}/bhm/memory", params={"id": memory_id, "project": project})
    response.raise_for_status()
    return response.json()["memory"]


def get_links(client: httpx.Client, base_url: str, memory_id: str, project: str) -> list[dict[str, Any]]:
    response = client.get(f"{base_url}/bhm/memory/links", params={"id": memory_id, "project": project})
    response.raise_for_status()
    return list(response.json().get("links") or [])


def verify_run(run_dir: Path, base_url: str) -> dict[str, Any]:
    worker_data = extract_worker_stdout(run_dir / "worker.log")
    harvest = worker_data["harvest"]
    crystal = worker_data["crystal"]
    links_plan = worker_data["links"]
    project = harvest["project"]
    source_ids = [str(item) for item in harvest["source_ids"]]
    crystal_id = str(crystal["id"])
    is_dry_run = crystal_id == "<dry-run>"

    report: dict[str, Any] = {
        "run_dir": str(run_dir),
        "project": project,
        "dry_run": is_dry_run,
        "crystal_id": crystal_id,
        "source_count": len(source_ids),
        "checks": [],
        "ok": True,
    }

    def check(name: str, ok: bool, detail: Any = None) -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["ok"] = False

    check("source_count_15_to_25", 15 <= len(source_ids) <= 25, len(source_ids))
    check("crystal_metadata_fact", crystal["metadata"].get("semantic_type") == "fact", crystal["metadata"])
    check("crystal_lifecycle_validated", crystal["metadata"].get("lifecycle") == "validated")

    if is_dry_run:
        return report

    with _bhm_client() as client:
        crystal_record = get_memory(client, base_url, crystal_id, project)
        crystal_metadata = crystal_record.get("metadata") or {}
        check("crystal_exists", crystal_record.get("id") == crystal_id, crystal_record.get("id"))
        check("crystal_is_validated_fact", crystal_metadata.get("lifecycle") == "validated" and crystal_metadata.get("semantic_type") == "fact", crystal_metadata)

        archived = []
        not_archived = []
        for source_id in source_ids:
            memory = get_memory(client, base_url, source_id, project)
            metadata = memory.get("metadata") or {}
            if metadata.get("lifecycle") == "archived" and memory.get("archived_at"):
                archived.append(source_id)
            else:
                not_archived.append({"id": source_id, "metadata": metadata, "archived_at": memory.get("archived_at")})
        check("sources_archived", len(archived) == len(source_ids), {"archived": len(archived), "not_archived": not_archived})

        actual_links = get_links(client, base_url, crystal_id, project)
        condenses_targets = {str(link.get("target_id")) for link in actual_links if link.get("relation") == "condenses"}
        check("condenses_links_cover_sources", set(source_ids).issubset(condenses_targets), {"expected": len(source_ids), "actual": len(condenses_targets)})

        project_hub_id = links_plan.get("project_hub_id")
        if project_hub_id:
            hub_links = [
                link for link in actual_links
                if link.get("relation") == "belongs_to_project_hub" and str(link.get("target_id")) == str(project_hub_id)
            ]
            check("project_hub_link_exists", len(hub_links) >= 1, {"project_hub_id": project_hub_id})

    return report


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a BHM crystallizer run directory")
    parser.add_argument("--run-dir", required=True)
    parser.add_argument("--bhm-base-url", default=endpoint_url("bhm_api"))
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = parse_args(argv or sys.argv[1:])
    report = verify_run(Path(args.run_dir), args.bhm_base_url.rstrip("/"))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
