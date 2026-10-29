#!/usr/bin/env python3
"""Read-only BHM memory inventory: what memory knows across projects."""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any

SCHEMA_VERSION = "bhm.memory-inventory.v1"
KNOWLEDGE_TYPES = frozenset({
    "architecture", "bug", "bugfix", "decision", "fact", "howto", "knowledge",
    "knowledge-crystal", "lesson", "pattern", "pitfall", "playbook", "reference",
    "rule", "runbook", "state", "workflow",
})
NOISE_TYPES = frozenset({
    "audit", "checkpoint", "diagnostic", "error", "hybrid", "log", "observation",
    "receipt", "release-receipt", "session", "trace", "transient-context", "validation",
})
PLAYBOOK_RULE_TYPES = frozenset({"playbook", "rule"})
REQUIRED_MEMORIES_COLUMNS = {"memory_id", "project", "memory_type", "lifecycle", "title", "created_at", "updated_at"}
REQUIRED_REVISIONS_COLUMNS = {"revision_id", "memory_id"}


class MemoryInventoryError(Exception):
    """Raised when SQLite database is missing, unreadable, or invalid."""


def _default_database() -> Path:
    try:
        from blackholememory.config import settings
        return Path(settings.runtime_dir) / "live-memory" / "memories.sqlite3"
    except Exception:
        return Path(__file__).resolve().parents[1] / ".runtime" / "live-memory" / "memories.sqlite3"


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value or not str(value).strip():
        return None
    raw = str(value).strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def build_inventory(
    database_path: str | Path,
    *,
    project: str | None = None,
    recent_limit: int = 8,
    stale_days: int = 14,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    safe_path = Path(database_path).resolve()
    if not safe_path.is_file():
        raise MemoryInventoryError(f"файл базы данных не найден: {safe_path.name}")

    try:
        connection = sqlite3.connect(f"file:{safe_path.as_posix()}?mode=ro", uri=True, timeout=5.0)
    except (sqlite3.Error, OSError) as exc:
        raise MemoryInventoryError(f"ошибка подключения к базе: {exc}") from exc

    with closing(connection):
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only=ON")
            if str(connection.execute("PRAGMA quick_check").fetchone()[0]).casefold() != "ok":
                raise MemoryInventoryError("PRAGMA quick_check завершилась ошибкой")
            tables = {str(r[0]) for r in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()}
            if not {"memories", "memory_revisions"}.issubset(tables):
                raise MemoryInventoryError("в базе отсутствуют необходимые таблицы (memories, memory_revisions)")
            m_cols = {str(r[1]) for r in connection.execute("PRAGMA table_info(memories)").fetchall()}
            r_cols = {str(r[1]) for r in connection.execute("PRAGMA table_info(memory_revisions)").fetchall()}
            if not REQUIRED_MEMORIES_COLUMNS.issubset(m_cols) or not REQUIRED_REVISIONS_COLUMNS.issubset(r_cols):
                raise MemoryInventoryError("в схеме таблиц отсутствуют необходимые колонки")
        except sqlite3.Error as exc:
            raise MemoryInventoryError(f"ошибка проверки схемы базы данных: {exc}") from exc

        if project is not None:
            target_projects = [str(project).strip()]
        else:
            rows = connection.execute(
                "SELECT DISTINCT project FROM memories WHERE coalesce(project, '') != '' ORDER BY project"
            ).fetchall()
            target_projects = [str(r[0]) for r in rows]

        now = as_of or datetime.now(timezone.utc)
        stale_sec = max(0, stale_days) * 86_400
        projects_data: list[dict[str, Any]] = []

        for proj in target_projects:
            records = connection.execute(
                "SELECT memory_id, memory_type, title, created_at, updated_at FROM memories "
                "WHERE project = ? AND coalesce(lifecycle, '') NOT IN ('archived', 'tombstoned', 'deleted')",
                (proj,),
            ).fetchall()

            active_cnt = len(records)
            t_counts = dict(sorted(Counter(str(r["memory_type"] or "") for r in records).items(), key=lambda i: (-i[1], i[0])))
            types_list = [{"type": t, "count": c} for t, c in t_counts.items()]
            k_cnt = sum(c for t, c in t_counts.items() if t in KNOWLEDGE_TYPES)
            n_cnt = sum(c for t, c in t_counts.items() if t in NOISE_TYPES)
            o_cnt = sum(c for t, c in t_counts.items() if t not in KNOWLEDGE_TYPES and t not in NOISE_TYPES)
            c_dates = [str(r["created_at"]) for r in records if r["created_at"]]
            a_dates = [str(r["updated_at"] or r["created_at"]) for r in records if (r["updated_at"] or r["created_at"])]

            sorted_r = sorted(records, key=lambda r: (str(r["updated_at"] or r["created_at"] or ""), str(r["memory_id"] or "")), reverse=True)
            recent_titles = [
                {"title": str(r["title"] or ""), "memory_type": str(r["memory_type"] or ""), "date": str(r["updated_at"] or r["created_at"] or "")}
                for r in sorted_r[:max(0, recent_limit)]
            ]

            warnings: list[dict[str, str]] = []
            if k_cnt < 5:
                warnings.append({"code": "low_knowledge", "message": "Мало знаний (< 5)"})
            if sum(t_counts.get(t, 0) for t in PLAYBOOK_RULE_TYPES) == 0:
                warnings.append({"code": "no_playbook_rule", "message": "Нет playbook/rule"})
            if any(
                str(r["memory_type"] or "") == "state"
                and (dt := parse_iso_datetime(r["updated_at"] or r["created_at"])) is not None
                and (now - dt).total_seconds() > stale_sec
                for r in records
            ):
                warnings.append({"code": "stale_state", "message": f"Устарели state (> {stale_days} дн.)"})
            if active_cnt > 0 and (n_cnt / active_cnt) > 0.70:
                warnings.append({"code": "high_noise", "message": "Много шума (> 70%)"})

            projects_data.append({
                "project": proj, "active_count": active_cnt, "type_counts": t_counts, "types": types_list,
                "shares": {
                    "knowledge_count": k_cnt, "knowledge_ratio": round(k_cnt / active_cnt, 4) if active_cnt else 0.0,
                    "noise_count": n_cnt, "noise_ratio": round(n_cnt / active_cnt, 4) if active_cnt else 0.0,
                    "other_count": o_cnt, "other_ratio": round(o_cnt / active_cnt, 4) if active_cnt else 0.0,
                },
                "oldest_record_at": min(c_dates) if c_dates else None,
                "newest_record_at": max(a_dates) if a_dates else None,
                "recent_titles": recent_titles, "warnings": warnings,
            })

    totals = {
        "projects_count": len(projects_data),
        "active_count": sum(p["active_count"] for p in projects_data),
        "knowledge_count": sum(p["shares"]["knowledge_count"] for p in projects_data),
        "noise_count": sum(p["shares"]["noise_count"] for p in projects_data),
        "other_count": sum(p["shares"]["other_count"] for p in projects_data),
        "warnings_count": sum(len(p["warnings"]) for p in projects_data),
    }
    return {"schema": SCHEMA_VERSION, "generated_at": now.isoformat(), "database": safe_path.name, "totals": totals, "projects": projects_data}


def format_markdown(data: dict[str, Any]) -> str:
    lines = [
        "# Инвентаризация памяти BHM", "",
        f"- **База данных**: `{data.get('database', '')}`",
        f"- **Всего проектов**: {data.get('totals', {}).get('projects_count', 0)}",
        f"- **Всего активных записей**: {data.get('totals', {}).get('active_count', 0)}",
        f"- **Дата формирования**: {data.get('generated_at', '')}", "",
        "## Итоги по проектам", "",
        "| Проект | Активных | Знания | Шум | Прочие | Период активности | Предупреждения |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    projects = data.get("projects", [])
    if not projects:
        lines.append("| — | 0 | 0 (0.0%) | 0 (0.0%) | 0 (0.0%) | нет данных | нет |")
    for p in projects:
        sh = p["shares"]
        k_pct = f"{sh['knowledge_count']} ({sh['knowledge_ratio'] * 100:.1f}%)"
        n_pct = f"{sh['noise_count']} ({sh['noise_ratio'] * 100:.1f}%)"
        o_pct = f"{sh['other_count']} ({sh['other_ratio'] * 100:.1f}%)"
        o_d, n_d = (p["oldest_record_at"] or "")[:10], (p["newest_record_at"] or "")[:10]
        period = f"{o_d}..{n_d}" if o_d and n_d else "нет данных"
        warns = ", ".join(w["code"] for w in p["warnings"]) or "нет"
        lines.append(f"| {p['project']} | {p['active_count']} | {k_pct} | {n_pct} | {o_pct} | {period} | {warns} |")

    lines.extend(["", "## Детальная информация по проектам", ""])
    for p in projects:
        sh = p["shares"]
        lines.extend([
            f"### Проект: {p['project']}",
            f"- **Активных записей**: {p['active_count']}",
            f"- **Доли**: Знания: {sh['knowledge_count']} ({sh['knowledge_ratio'] * 100:.1f}%), "
            f"Шум: {sh['noise_count']} ({sh['noise_ratio'] * 100:.1f}%), Прочие: {sh['other_count']} ({sh['other_ratio'] * 100:.1f}%)",
            f"- **Даты записей**: самая старая: {p['oldest_record_at'] or 'нет'}, самая новая: {p['newest_record_at'] or 'нет'}",
            f"- **Разбивка по типам**: {', '.join(f'{t}: {c}' for t, c in p['type_counts'].items()) or 'нет записей'}",
            "- **Предупреждения**:",
        ])
        lines.extend([f"  - `[{w['code']}]` {w['message']}" for w in p["warnings"]] if p["warnings"] else ["  - нет"])
        lines.append(f"- **Последние заголовки** ({len(p['recent_titles'])}):")
        lines.extend(
            [f"  - `[{i['date'][:10]}]` `[{i['memory_type']}]` {i['title'] or '(без заголовка)'}" for i in p["recent_titles"]]
            if p["recent_titles"] else ["  - нет записей"]
        )
        lines.append("")
    return "\n".join(lines).strip() + "\n"


def format_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Инвентаризация памяти BHM (read-only): что память знает по проектам.")
    parser.add_argument("--database", type=Path, default=_default_database(), help="Путь к SQLite базе memories.sqlite3")
    parser.add_argument("--project", default=None, help="Имя проекта (если не указано, проверяются все проекты)")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown", help="Формат вывода (markdown или json)")
    parser.add_argument("--out", type=Path, default=None, help="Путь к выходному файлу (если не указано, stdout)")
    parser.add_argument("--recent", type=int, default=8, help="Количество последних заголовков на проект (по умолчанию 8)")
    parser.add_argument("--stale-days", type=int, default=14, help="Порог устаревания для записей state в днях (по умолчанию 14)")
    args = parser.parse_args(argv)

    try:
        report = build_inventory(args.database, project=args.project, recent_limit=args.recent, stale_days=args.stale_days)
    except MemoryInventoryError as exc:
        sys.stderr.write(f"Ошибка инвентаризации: {exc}\n")
        return 2
    except Exception as exc:
        sys.stderr.write(f"Непредвиденная ошибка: {exc}\n")
        return 2

    output = format_json(report) if args.format == "json" else format_markdown(report)
    if args.out:
        try:
            args.out.write_text(output, encoding="utf-8")
        except OSError as exc:
            sys.stderr.write(f"Ошибка записи в файл {args.out}: {exc}\n")
            return 2
    else:
        # Windows consoles default to a legacy code page that cannot encode the Russian report text.
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdout.write(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
