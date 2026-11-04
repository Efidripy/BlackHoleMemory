"""Safely backfill template card titles with deterministic titles derived from content."""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from backfill_doc2query import UPDATE_URL, backup_database, positive_integer  # noqa: E402

TEMPLATE_HEADER_RE = re.compile(
    r"^(?:#+\s*)?(?:[\w.-]+\s+)?(?:checkpoint|hybrid\s+session\s+record|workflow|transient-context|architecture|pattern|session\s+record)\s*:\s*$",
    re.IGNORECASE,
)
DONE_FIELD_RE = re.compile(r"^\s*[*#_`\s-]*\bdone\b[*#_`\s]*[:\-]", re.IGNORECASE)
SECTION_HEADER_RE = re.compile(
    r"^\s*[*#_`\s-]*\b(next|checks|risks|notes|title|project|details|context)\b[*#_`\s]*[:\-]",
    re.IGNORECASE,
)
MEANINGLESS_PHRASES = frozenset(
    {
        "checkpoint",
        "checkpoints",
        "hybrid session record",
        "session record",
        "session records",
        "workflow",
        "transient-context",
        "architecture",
        "pattern",
        "done",
        "next",
        "checks",
        "risks",
        "notes",
        "none",
        "n/a",
        "na",
        "todo",
        "tbd",
        "null",
        "nil",
    }
)


def extract_date(
    observed_at: str | None,
    created_at: str | None,
    metadata_json: str | None,
) -> str:
    """Extract YYYY-MM-DD from observed_at, metadata_json, or created_at."""
    if observed_at and isinstance(observed_at, str):
        match = re.search(r"\b\d{4}-\d{2}-\d{2}", observed_at)
        if match:
            return match.group(0)

    if metadata_json and isinstance(metadata_json, str):
        try:
            meta = json.loads(metadata_json)
            if isinstance(meta, dict):
                val = meta.get("observed_at")
                if val and isinstance(val, str):
                    match = re.search(r"\b\d{4}-\d{2}-\d{2}", val)
                    if match:
                        return match.group(0)
        except (json.JSONDecodeError, TypeError):
            pass

    if created_at and isinstance(created_at, str):
        match = re.search(r"\b\d{4}-\d{2}-\d{2}", created_at)
        if match:
            return match.group(0)

    if metadata_json and isinstance(metadata_json, str):
        try:
            meta = json.loads(metadata_json)
            if isinstance(meta, dict):
                for key in ("created_at", "created"):
                    val = meta.get(key)
                    if val and isinstance(val, str):
                        match = re.search(r"\b\d{4}-\d{2}-\d{2}", val)
                        if match:
                            return match.group(0)
        except (json.JSONDecodeError, TypeError):
            pass

    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def is_template_line(line: str, old_title: str | None = None) -> bool:
    """Check if a line matches a generic or specific template header."""
    stripped = line.strip()
    if old_title:
        norm_title = old_title.strip().casefold()
        norm_line = stripped.casefold()
        if norm_line == norm_title or norm_line == norm_title.rstrip(":"):
            return True
        clean_norm_line = re.sub(r"^#+\s*", "", norm_line).strip()
        if clean_norm_line == norm_title or clean_norm_line == norm_title.rstrip(":"):
            return True
    return bool(TEMPLATE_HEADER_RE.match(stripped))


def extract_first_sentence(text: str) -> str:
    """Extract the first meaningful sentence from a text block."""
    cleaned = text.strip()
    cleaned = re.sub(r"^(?:[*#_`\s-]+|\d+[.)])\s*", "", cleaned).strip()
    if not cleaned:
        return ""
    parts = re.split(r"(?<=[.!?])\s+|\n+", cleaned)
    for part in parts:
        candidate = part.strip()
        candidate = re.sub(r"^(?:[*#_`\s-]+|\d+[.)])\s*", "", candidate).strip()
        if candidate:
            return candidate
    return ""


def has_done_field(content: str) -> bool:
    """Check whether content has an explicit done:/Done: section."""
    for line in content.splitlines():
        if DONE_FIELD_RE.match(line):
            return True
    return False


def extract_done_sentence(content: str) -> str | None:
    """Extract the first sentence following a done:/Done: field."""
    lines = content.splitlines()
    done_idx = None
    for idx, line in enumerate(lines):
        if DONE_FIELD_RE.match(line):
            done_idx = idx
            break
    if done_idx is None:
        return None

    rest = DONE_FIELD_RE.sub("", lines[done_idx]).strip()
    if rest:
        sentence = extract_first_sentence(rest)
        if sentence:
            return sentence

    collected_lines: list[str] = []
    for line in lines[done_idx + 1:]:
        stripped = line.strip()
        if not stripped:
            if collected_lines:
                break
            continue
        if SECTION_HEADER_RE.match(stripped) or stripped.startswith("#"):
            break
        collected_lines.append(stripped)

    if collected_lines:
        sentence = extract_first_sentence("\n".join(collected_lines))
        if sentence:
            return sentence
    return None


def extract_body_sentence(content: str, old_title: str | None = None) -> str | None:
    """Extract the first non-empty sentence from content body, skipping template headers."""
    lines = content.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        if is_template_line(stripped, old_title):
            continue
        cleaned = re.sub(r"^(?:#+\s*|[*#_`\s-]+\s*|\d+[.)]\s*)", "", stripped).strip()
        if not cleaned or is_template_line(cleaned, old_title):
            continue
        if re.match(r"^[\w/.-]+\s*:\s*$", cleaned):
            continue
        field_match = re.match(
            r"^(?:description|details|context|summary|pattern|architecture)\s*:\s*(.+)$",
            cleaned,
            re.IGNORECASE,
        )
        if field_match:
            cleaned = field_match.group(1).strip()
        sentence = extract_first_sentence(cleaned)
        if sentence:
            return sentence
    return None


def is_meaningless_sentence(sentence: str, project: str) -> bool:
    """Check if the extracted sentence is empty, too short, or only a template repetition."""
    clean = sentence.strip().rstrip(".:-").strip().casefold()
    if not clean or len(clean) < 3:
        return True
    if clean in MEANINGLESS_PHRASES:
        return True
    proj_clean = project.strip().casefold()
    if clean in {
        f"{proj_clean} checkpoint",
        f"{proj_clean} hybrid session record",
        f"{proj_clean} session record",
        f"{proj_clean} workflow",
        f"{proj_clean} transient-context",
        f"{proj_clean} architecture",
        f"{proj_clean} pattern",
    }:
        return True
    return False


def truncate_title(title: str, max_length: int = 110) -> str:
    """Truncate title to max_length along word boundaries, without cutting words."""
    if len(title) <= max_length:
        return title
    if title[max_length] == " ":
        return title[:max_length].rstrip(" ,;:-")
    truncated = title[:max_length]
    last_space = truncated.rfind(" ")
    if last_space > 0:
        return truncated[:last_space].rstrip(" ,;:-")
    return truncated.rstrip(" ,;:-")


def derive_new_title(
    project: str,
    content: str,
    date: str,
    old_title: str | None = None,
) -> str | None:
    """Derive a new deterministic title from memory content, or return None if meaningless."""
    if not isinstance(content, str) or not content.strip():
        return None

    if has_done_field(content):
        sentence = extract_done_sentence(content)
    else:
        sentence = extract_body_sentence(content, old_title)

    if not sentence:
        return None

    sentence = sentence.rstrip(":").strip()
    if is_meaningless_sentence(sentence, project):
        return None

    full_title = f"{date} {project}: {sentence}"
    candidate = truncate_title(full_title, max_length=110)
    if len(candidate) < 15:
        return None
    return candidate


def read_candidates(connection: sqlite3.Connection, projects: set[str] | None, limit: int):
    """Find active memories with template titles ending in ':' and derive new titles."""
    cols = {row[1] for row in connection.execute("PRAGMA table_info(memories)").fetchall()}
    select_cols = ["m.memory_id", "m.project", "m.lifecycle", "m.title"]
    if "observed_at" in cols:
        select_cols.append("m.observed_at")
    else:
        select_cols.append("NULL AS observed_at")

    if "created_at" in cols:
        select_cols.append("m.created_at")
    elif "created" in cols:
        select_cols.append("m.created AS created_at")
    else:
        select_cols.append("NULL AS created_at")

    if "metadata_json" in cols:
        select_cols.append("m.metadata_json")
    else:
        select_cols.append("NULL AS metadata_json")

    select_cols.append("r.content")

    query = (
        f"SELECT {', '.join(select_cols)} "
        "FROM memories m JOIN memory_revisions r "
        "ON m.current_revision_id = r.revision_id AND m.memory_id = r.memory_id "
        "ORDER BY m.memory_id"
    )
    rows = connection.execute(query)

    candidates = []
    skipped_filtered = 0
    skipped_meaningless = 0

    for memory_id, project, lifecycle, title, observed_at, created_at, metadata_json, content in rows:
        if projects and project not in projects:
            skipped_filtered += 1
            continue
        if lifecycle != "active":
            skipped_filtered += 1
            continue
        if not isinstance(title, str) or not title.strip().endswith(":"):
            skipped_filtered += 1
            continue
        if not isinstance(content, str):
            skipped_meaningless += 1
            continue

        date = extract_date(observed_at, created_at, metadata_json)
        new_title = derive_new_title(project, content, date, old_title=title)
        if not new_title:
            skipped_meaningless += 1
            continue

        if len(candidates) < limit:
            candidates.append(
                {
                    "memory_id": memory_id,
                    "project": project,
                    "title": title,
                    "old_title": title,
                    "new_title": new_title,
                }
            )

    return candidates, skipped_filtered, skipped_meaningless


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--limit", type=positive_integer, default=50)
    parser.add_argument("--project", "--projects", action="append", dest="projects")
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

    projects = None
    if args.projects:
        projects = set()
        for p in args.projects:
            for item in p.split(","):
                val = item.strip()
                if val:
                    projects.add(val)

    with sqlite3.connect(args.db.resolve().as_uri() + "?mode=ro", uri=True) as source:
        candidates, filtered, skipped = read_candidates(source, projects, args.limit)
        if args.apply:
            backup_database(source, args.backup)

    report: dict[str, object] = {
        "skipped": skipped,
        "skipped_filtered": filtered,
        "items": [],
    }

    consecutive_errors = 0
    items_list: list[dict[str, object]] = []
    for item in candidates:
        entry: dict[str, object] = {
            "memory_id": item["memory_id"],
            "project": item["project"],
            "title": item["title"],
            "old_title": item["old_title"],
            "new_title": item["new_title"],
        }
        items_list.append(entry)

        if not args.apply:
            continue

        if consecutive_errors >= 3:
            entry["status"] = "not_attempted"
            continue

        try:
            response = httpx.post(
                UPDATE_URL,
                json={"id": item["memory_id"], "project": item["project"], "title": item["new_title"]},
                headers={"Authorization": f"Bearer {os.environ['BHM_CALLER_TOKEN']}"},
                timeout=20.0,
            )
            success = response.status_code == 200
        except httpx.HTTPError:
            success = False

        entry["status"] = "ok" if success else "failed"
        consecutive_errors = 0 if success else consecutive_errors + 1

    report["items"] = items_list
    args.out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
