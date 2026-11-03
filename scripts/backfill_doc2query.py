"""Safely backfill doc2query questions through the memory update API."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from blackholememory.app_core_memory_base import _build_memory_title  # noqa: E402
from blackholememory.write_enrichment import enrich_content  # noqa: E402

EXCLUDED_TYPES = frozenset({
    "workflow", "checkpoint", "transient-context", "session-record", "log",
    "session_preflight", "security_scan_checkpoint", "diagnostic",
})
UPDATE_URL = "http://127.0.0.1:8000/bhm/memory/update"


def positive_integer(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def read_candidates(connection, projects, limit):
    rows = connection.execute(
        "SELECT m.memory_id, m.project, m.memory_type, m.lifecycle, m.title, r.content "
        "FROM memories m JOIN memory_revisions r ON m.current_revision_id = r.revision_id "
        "AND m.memory_id = r.memory_id ORDER BY m.memory_id"
    )
    candidates = []
    filtered = 0
    mismatch = 0
    for memory_id, project, memory_type, lifecycle, title, content in rows:
        if projects and project not in projects:
            continue
        if (lifecycle != "active" or memory_type in EXCLUDED_TYPES
                or not isinstance(content, str) or not 200 <= len(content) <= 6000
                or "Вопросы:" in content):
            filtered += 1
        elif not isinstance(title, str) or _build_memory_title(content).strip() != title.strip():
            mismatch += 1
        elif len(candidates) < limit:
            candidates.append({"memory_id": memory_id, "project": project,
                               "title": title, "content": content})
    return candidates, filtered, mismatch


def backup_database(source, path):
    with path.open("xb"):
        pass
    with sqlite3.connect(path) as destination:
        source.backup(destination)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=positive_integer, default=50)
    parser.add_argument("--project", action="append")
    parser.add_argument("--concurrency", type=positive_integer, default=3)
    parser.add_argument("--backup", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    if args.apply and not args.backup:
        parser.error("--apply requires --backup")
    if args.apply and not os.getenv("BHM_CALLER_TOKEN", "").strip():
        parser.error("--apply requires BHM_CALLER_TOKEN")
    if args.backup and args.out.resolve() == args.backup.resolve():
        parser.error("--out and --backup must be different files")
    if args.out.resolve() == args.db.resolve():
        parser.error("--out and --db must be different files")

    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as source:
        candidates, filtered, mismatch = read_candidates(source, args.project, args.limit)
        if args.apply:
            backup_database(source, args.backup)

    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        enriched = list(executor.map(lambda item: enrich_content(item["title"], item["content"]), candidates))

    report = {"skipped_title_mismatch": mismatch, "skipped_filtered": filtered,
              "enrich_failed": 0, "items": []}
    consecutive_errors = 0
    for item, new_content in zip(candidates, enriched):
        original = item["content"]
        entry = {"memory_id": item["memory_id"], "project": item["project"],
                 "title": item["title"], "original_length": len(original), "questions": None}
        report["items"].append(entry)
        if not new_content.startswith(original + "\n\nВопросы: "):
            report["enrich_failed"] += 1
            if args.apply:
                entry["status"] = "enrich_failed"
            continue
        entry["questions"] = new_content[len(original):].strip()
        if not args.apply:
            continue
        if consecutive_errors >= 3:
            entry["status"] = "not_attempted"
            continue
        try:
            response = httpx.post(
                UPDATE_URL,
                json={"id": item["memory_id"], "project": item["project"], "content": new_content},
                headers={"Authorization": f"Bearer {os.environ['BHM_CALLER_TOKEN']}"},
                timeout=20.0,
            )
            success = response.status_code == 200
        except httpx.HTTPError:
            success = False
        entry["status"] = "ok" if success else "failed"
        consecutive_errors = 0 if success else consecutive_errors + 1

    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
