from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .memory_service import SQLiteMemoryService
from .observation_store import ObservationStore


REPO_ROOT = Path(__file__).resolve().parents[2]
LIVE_MEMORY_DIR = REPO_ROOT / "runtime" / "live-memory"
LESSON_FILE = LIVE_MEMORY_DIR / "lessons.json"
SLOT_FILE = LIVE_MEMORY_DIR / "slots.json"
OBSERVATION_STORE_FILE = LIVE_MEMORY_DIR / "observations.sqlite3"
MEMORY_DATABASE_FILE = LIVE_MEMORY_DIR / "memories.sqlite3"

TAG_STOPWORDS = {
    "bhm",
    "workspace",
    "memory",
    "checkpoint",
    "session",
    "project",
    "notes",
    "record",
    "archive",
}

TYPE_COLORS = {
    "project": "#f7f7f2",
    "memory_type": "#4bd1ff",
    "tag": "#ffb347",
    "memory": "#87f5c9",
    "lesson": "#ffd166",
    "slot": "#8a6cff",
    "observation": "#ff6b8a",
}

MEMORY_TYPE_COLORS = {
    "workflow":     "#4bd1ff",
    "checkpoint":   "#87f5c9",
    "pattern":      "#ffb347",
    "fact":         "#ffd166",
    "decision":     "#c9b1ff",
    "bug":          "#ff6b8a",
    "architecture": "#f7f7f2",
    "runbook":      "#ff9de2",
    "handoff":      "#ff4444",
    "session":      "#a8d8a8",
    "lesson":       "#ffd166",
}

PROJECT_COLORS = {
    "multiserversubgen":            "#87f5c9",
    "blackholememory":              "#4bd1ff",
    "e-github-workspace":           "#a8d8a8",
    "lnv-push":                     "#ffd166",
    "sojmieblo":                    "#ff6b8a",
    "agent-memory-codex-connector": "#b1d4ff",
    "agent-ops":                    "#ffb347",
    "figma-design":                 "#ff9de2",
}

VALID_DOMAINS = {"frontend", "backend", "infra", "security", "product"}
UNKNOWN_DOMAIN_VALUES = {"", "unknown", "no metadata", "none", "null", "n/a", "not set"}
DOMAIN_COLORS = {
    "frontend": "#3b82f6",
    "backend": "#ef4444",
    "infra": "#a855f7",
    "security": "#f97316",
    "product": "#22c55e",
    "unknown": "#94a3b8",
}
DOMAIN_PATTERNS = {
    "frontend": (
        r"\bthree(?:\.js|js)?\b",
        r"\bcanvas\b",
        r"\bhtml\b",
        r"\bstatic[\\/]",
        r"\.html?\b",
        r"\bfrontend\b",
        r"\bcss\b",
        r"\breact\b",
        r"\btsx?\b",
    ),
    "backend": (
        r"\bfastapi\b",
        r"\broutes?\b",
        r"\bendpoint\b",
        r"\bapp[\\/]routes\b",
        r"\bbackend\b",
        r"\bapi\b",
        r"\bservice\b",
    ),
    "infra": (
        r"\bdocker\b",
        r"\bqdrant\b",
        r"\basyncio\b",
        r"\bnpipe\b",
        r"\bdb_connection\b",
        r"\bmem0\b",
        r"\bmcp\b",
        r"\bruntime\b",
        r"\bworker\b",
        r"\bpowershell\b",
    ),
    "security": (
        r"\bsecurity\b",
        r"\bsecret\b",
        r"\btoken\b",
        r"\bpassword\b",
        r"\bcredential\b",
        r"\bauth(?:entication|orization)?\b",
        r"\bvulnerab",
        r"\bcve-\d",
    ),
    "product": (
        r"\bproduct\b",
        r"\brequirements?\b",
        r"\bux\b",
        r"\boperator\b",
        r"\bworkflow\b",
        r"\broadmap\b",
        r"\buser stor(?:y|ies)\b",
        r"\bacceptance criteria\b",
    ),
}
DOMAIN_PRIORITY = ("security", "frontend", "backend", "infra", "product")

PRIORITY_ALIASES = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "normal": "medium",
    "low": "low",
    "trivial": "low",
}
DEFAULT_NODE_PRIORITY = "medium"


def _normalize_priority(value: Any, default: str = DEFAULT_NODE_PRIORITY) -> str:
    normalized = str(value or "").strip().lower()
    return PRIORITY_ALIASES.get(normalized, default)


def _domain_fragment(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_domain_fragment(item) for item in value)
    if isinstance(value, dict):
        try:
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
        except TypeError:
            return str(value)
    return str(value)


def infer_domain(text: Any = "", metadata_domain: Any = None, files: Any = None) -> str:
    domain = str(metadata_domain or "").strip().lower()
    if domain in VALID_DOMAINS:
        return domain
    if domain and domain not in UNKNOWN_DOMAIN_VALUES:
        return "infra"

    lowered = "\n".join(
        fragment
        for fragment in (_domain_fragment(text), _domain_fragment(files))
        if fragment
    ).lower()[:12000]
    scores: dict[str, int] = {}
    for candidate, patterns in DOMAIN_PATTERNS.items():
        score = sum(1 for pattern in patterns if re.search(pattern, lowered, re.IGNORECASE))
        if score:
            scores[candidate] = score
    if not scores:
        return "infra"
    return max(DOMAIN_PRIORITY, key=lambda candidate: (scores.get(candidate, 0), -DOMAIN_PRIORITY.index(candidate)))


@dataclass
class GalaxyOptions:
    project: str | None = None
    limit: int = 220
    tag_limit: int = 24
    include_tags: bool = True
    include_observations: bool = True


def _safe_load_json(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or "node"


def _memory_label(item: dict[str, Any]) -> str:
    metadata = item.get("metadata") or {}
    title = (metadata.get("raw_title") or "").strip()
    if title:
        return title[:72]
    content = (item.get("content") or item.get("memory") or "").strip()
    if not content:
        return item.get("source_id") or item.get("id") or "memory"
    first_line = content.splitlines()[0].strip()
    return first_line[:72]


def _memory_kind(item: dict[str, Any]) -> str:
    source_system = item.get("source_system")
    memory_type = item.get("memory_type") or (item.get("metadata") or {}).get("memory_type")
    if source_system == "obsidian-lessons" or memory_type == "lesson":
        return "lesson"
    return "memory"


def _normalize_memory_records(
    source_records: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    source_items = source_records
    if source_items is None:
        source_items = SQLiteMemoryService(MEMORY_DATABASE_FILE).load_records()
    for item in source_items:
        source_id = item.get("source_id") or item.get("id")
        if not source_id or source_id in seen_ids:
            continue
        seen_ids.add(source_id)
        metadata = dict(item.get("metadata") or {})
        project = item.get("project") or metadata.get("project") or "unscoped"
        memory_type = item.get("memory_type") or metadata.get("memory_type") or "memory"
        source_system = item.get("source_system") or metadata.get("source_system") or "bhm"
        content = item.get("content") or item.get("memory") or ""
        tags = item.get("tags") or metadata.get("tags") or []
        files = metadata.get("files") or []
        domain = infer_domain(
            [project, memory_type, source_system, tags, content, metadata.get("raw_title")],
            metadata.get("domain") or item.get("domain"),
            files,
        )
        metadata["domain"] = domain
        record = {
            "id": source_id,
            "project": project,
            "memory_type": memory_type,
            "source_system": source_system,
            "content": content,
            "tags": tags,
            "session_refs": item.get("session_refs") or metadata.get("session_refs") or [],
            "files": files,
            "metadata": metadata,
            "domain": domain,
            "priority": _normalize_priority(item.get("priority") or metadata.get("priority")),
            "kind": _memory_kind(item),
        }
        records.append(record)

    for item in _safe_load_json(LESSON_FILE):
        lesson_id = item.get("id")
        if not lesson_id or lesson_id in seen_ids:
            continue
        seen_ids.add(lesson_id)
        content = item.get("content") or ""
        domain = infer_domain([content, item.get("context"), "lesson"], item.get("domain"))
        records.append(
            {
                "id": lesson_id,
                "project": item.get("project") or "e-github-workspace",
                "memory_type": "lesson",
                "source_system": "bhm-live-lessons",
                "content": content,
                "tags": item.get("tags") or [],
                "session_refs": [],
                "files": [],
                "metadata": {"raw_title": (item.get("context") or item.get("content") or "")[:72], "domain": domain},
                "domain": domain,
                "priority": DEFAULT_NODE_PRIORITY,
                "kind": "lesson",
            }
        )

    return records


def _normalize_slots() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _safe_load_json(SLOT_FILE):
        label = item.get("label")
        if not label:
            continue
        domain = infer_domain([label, item.get("description"), item.get("content")], item.get("domain"))
        items.append(
            {
                "id": f"slot::{item.get('project') or 'workspace'}::{label}",
                "project": item.get("project") or "e-github-workspace",
                "label": label,
                "content": item.get("content") or "",
                "description": item.get("description") or "",
                "size_limit": int(item.get("sizeLimit") or 2000),
                "domain": domain,
            }
        )
    return items


def _normalize_observations() -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for item in _load_observation_records():
        obs_id = item.get("id")
        if not obs_id:
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
        security = metadata.get("security") if isinstance(metadata.get("security"), dict) else {}
        sensitivity = str(item.get("sensitivity") or security.get("sensitivity") or "internal")
        domain = infer_domain(
            [
                item.get("hookType"),
                item.get("cwd"),
                data.get("tool_name"),
                data.get("tool_input"),
                data.get("error"),
                data.get("stderr"),
                data.get("stdout"),
                data.get("command"),
            ],
            item.get("domain"),
            [item.get("cwd")],
        )
        items.append(
            {
                "id": obs_id,
                "project": item.get("project") or "e-github-workspace",
                "hook_type": item.get("hookType") or "observe",
                "session_id": item.get("sessionId") or "",
                "cwd": item.get("cwd") or "",
                "domain": domain,
                "payload_state": item.get("payloadState") or "raw",
                "sensitivity": sensitivity,
            }
        )
    return items


def _load_observation_records() -> list[dict[str, Any]]:
    store = ObservationStore(OBSERVATION_STORE_FILE)
    return store.load()


def _normalized_tags(record: dict[str, Any]) -> set[str]:
    return {
        str(tag).strip().lower()
        for tag in (record.get("tags") or [])
        if str(tag).strip()
    }


def _normalized_files(record: dict[str, Any]) -> set[str]:
    return {
        str(file_path).strip().lower()
        for file_path in (record.get("files") or [])
        if str(file_path).strip()
    }


def _normalized_session_refs(record: dict[str, Any]) -> set[str]:
    return {
        str(ref).strip().lower()
        for ref in (record.get("session_refs") or [])
        if str(ref).strip()
    }


def _record_domain(record: dict[str, Any]) -> str:
    metadata = record.get("metadata") or {}
    return infer_domain(
        [record.get("project"), record.get("memory_type"), record.get("tags"), record.get("content")],
        record.get("domain") or metadata.get("domain"),
        record.get("files") or metadata.get("files"),
    )


def _majority_domain(records: list[dict[str, Any]], fallback: Any) -> str:
    counts = Counter(_record_domain(record) for record in records if record)
    if counts:
        return counts.most_common(1)[0][0]
    return infer_domain(fallback)


def _aggregate_observations(items: list[dict[str, Any]], limit: int = 18) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], dict[str, Any]] = {}

    for item in items:
        key = (item["project"], item["hook_type"])
        current = grouped.get(key)
        if current is None:
            grouped[key] = {
                "id": f"observe::{item['project']}::{_slugify(item['hook_type'])}",
                "project": item["project"],
                "hook_type": item["hook_type"],
                "count": 1,
                "sample_session_ids": [item["session_id"]] if item["session_id"] else [],
                "sample_cwds": [item["cwd"]] if item["cwd"] else [],
                "domain_counts": Counter([item["domain"]]),
                "sensitivity_counts": Counter([item["sensitivity"]]),
                "sanitized_count": 1 if item["payload_state"] == "sanitized" else 0,
            }
            continue

        current["count"] += 1
        current["domain_counts"][item["domain"]] += 1
        current["sensitivity_counts"][item["sensitivity"]] += 1
        if item["payload_state"] == "sanitized":
            current["sanitized_count"] += 1
        if item["session_id"] and item["session_id"] not in current["sample_session_ids"] and len(current["sample_session_ids"]) < 4:
            current["sample_session_ids"].append(item["session_id"])
        if item["cwd"] and item["cwd"] not in current["sample_cwds"] and len(current["sample_cwds"]) < 3:
            current["sample_cwds"].append(item["cwd"])

    ranked = sorted(grouped.values(), key=lambda item: (-item["count"], item["project"], item["hook_type"]))
    for item in ranked:
        domain_counts = item.pop("domain_counts", Counter())
        sensitivity_counts = item.pop("sensitivity_counts", Counter())
        item["domain"] = domain_counts.most_common(1)[0][0] if domain_counts else infer_domain(item["hook_type"])
        item["sensitivity"] = sensitivity_counts.most_common(1)[0][0] if sensitivity_counts else "internal"
        item["restricted_count"] = int(sensitivity_counts.get("restricted", 0))
    return ranked[:limit]


def _score_memory(record: dict[str, Any]) -> float:
    score = 1.0
    if record["kind"] == "lesson":
        score += 3.0
    if record["memory_type"] in {"architecture", "pattern", "fact", "bug"}:
        score += 2.0
    if "workspace-layers" in set(record.get("tags") or []):
        score += 2.5
    if record.get("files"):
        score += 1.2
    if record.get("session_refs"):
        score += 0.7
    score += min(len(record.get("content") or "") / 600.0, 2.0)
    return score


def _memory_val(record: dict[str, Any]) -> float:
    return round(2.8 + min(_score_memory(record), 7.0), 2)


def _tag_rank(records: list[dict[str, Any]], tag_limit: int) -> set[str]:
    counts: Counter[str] = Counter()
    for record in records:
        for tag in record.get("tags") or []:
            normalized = str(tag).strip().lower()
            if not normalized or normalized in TAG_STOPWORDS:
                continue
            counts[normalized] += 1
    return {tag for tag, _ in counts.most_common(tag_limit)}


def build_galaxy_graph(
    options: GalaxyOptions,
    *,
    memory_records: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    memory_records = _normalize_memory_records(memory_records)
    if options.project:
        memory_records = [item for item in memory_records if item["project"] == options.project]

    memory_records.sort(key=_score_memory, reverse=True)
    memory_records = memory_records[: max(10, options.limit)]

    slots = _normalize_slots()
    observations = _normalize_observations()
    if options.project:
        slots = [item for item in slots if item["project"] == options.project]
        observations = [item for item in observations if item["project"] == options.project]
    observations = _aggregate_observations(observations, limit=16 if options.project else 24)

    top_tags = _tag_rank(memory_records, options.tag_limit) if options.include_tags else set()
    domain_records = memory_records + slots + observations
    project_domains = {
        project: _majority_domain([record for record in domain_records if record.get("project") == project], project)
        for project in sorted({item["project"] for item in domain_records})
    }
    type_domains = {
        memory_type: _majority_domain([record for record in memory_records if record.get("memory_type") == memory_type], memory_type)
        for memory_type in sorted({item["memory_type"] for item in memory_records})
    }
    tag_domains = {
        tag: _majority_domain([record for record in memory_records if tag in _normalized_tags(record)], tag)
        for tag in top_tags
    }

    nodes: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    seen_nodes: set[str] = set()
    seen_links: set[tuple[str, str, str]] = set()

    def add_node(node: dict[str, Any]) -> None:
        meta = node.get("meta") if isinstance(node.get("meta"), dict) else {}
        metadata = node.get("metadata") or meta.get("metadata") or {}
        domain = infer_domain(
            [node.get("label"), node.get("type"), meta, metadata],
            node.get("domain") or metadata.get("domain") or meta.get("domain"),
            meta.get("files") or metadata.get("files"),
        )
        if isinstance(node.get("metadata"), dict):
            node["metadata"]["domain"] = domain
        node_meta = node.setdefault("meta", {})
        if isinstance(node_meta, dict):
            node_meta["domain"] = domain
            if isinstance(node_meta.get("metadata"), dict):
                node_meta["metadata"]["domain"] = domain
        node["priority"] = _normalize_priority(node.get("priority") or metadata.get("priority"))
        node_id = node["id"]
        if node_id in seen_nodes:
            return
        seen_nodes.add(node_id)
        nodes.append(node)

    def add_link(source: str, target: str, kind: str, weight: float = 1.0) -> None:
        key = (source, target, kind)
        if key in seen_links:
            return
        seen_links.add(key)
        links.append(
            {
                "source": source,
                "target": target,
                "kind": kind,
                "weight": round(weight, 2),
            }
        )

    memory_node_ids: dict[str, str] = {}

    projects = sorted({item["project"] for item in memory_records} | {item["project"] for item in slots} | {item["project"] for item in observations})
    for project in projects:
        project_id = f"project::{project}"
        add_node(
            {
                "id": project_id,
                "label": project,
                "type": "project",
                "val": 16,
                "color": TYPE_COLORS["project"],
                "meta": {
                    "project": project,
                    "kind": "project",
                    "domain": project_domains.get(project) or infer_domain(project),
                },
            }
        )

    type_nodes: dict[str, str] = {}
    for memory_type in sorted({item["memory_type"] for item in memory_records}):
        node_id = f"type::{_slugify(memory_type)}"
        type_nodes[memory_type] = node_id
        add_node(
            {
                "id": node_id,
                "label": memory_type,
                "type": "memory_type",
                "val": 7,
                "color": MEMORY_TYPE_COLORS.get(memory_type, TYPE_COLORS["memory_type"]),
                "meta": {
                    "memory_type": memory_type,
                    "kind": "memory_type",
                    "domain": type_domains.get(memory_type) or infer_domain(memory_type),
                },
            }
        )

    tag_nodes: dict[str, str] = {}
    for tag in sorted(top_tags):
        node_id = f"tag::{_slugify(tag)}"
        tag_nodes[tag] = node_id
        add_node(
            {
                "id": node_id,
                "label": tag,
                "type": "tag",
                "val": 5,
                "color": TYPE_COLORS["tag"],
                "meta": {
                    "tag": tag,
                    "kind": "tag",
                    "domain": tag_domains.get(tag) or infer_domain(tag),
                },
            }
        )

    for record in memory_records:
        node_type = record["kind"]
        node_id = f"memory::{record['id']}"
        metadata = record.get("metadata") or {}
        node_color = PROJECT_COLORS.get(record["project"], TYPE_COLORS.get(node_type, TYPE_COLORS["memory"]))
        add_node(
            {
                "id": node_id,
                "label": _memory_label(record),
                "type": node_type,
                "val": _memory_val(record),
                "color": node_color,
                "metadata": metadata,
                "priority": _normalize_priority(record.get("priority") or metadata.get("priority")),
                "meta": {
                    "project": record["project"],
                    "memory_type": record["memory_type"],
                    "source_id": record["id"],
                    "source_system": record["source_system"],
                    "domain": record["domain"],
                    "tags": record.get("tags") or [],
                    "files": record.get("files") or [],
                    "session_refs": record.get("session_refs") or [],
                    "raw_title": metadata.get("raw_title"),
                    "metadata": metadata,
                    "content_preview": (record.get("content") or "")[:320],
                },
            }
        )
        memory_node_ids[record["id"]] = node_id

        add_link(f"project::{record['project']}", node_id, "belongs_to_project", 2.8)
        add_link(type_nodes[record["memory_type"]], node_id, "has_memory_type", 1.8)

        for tag in record.get("tags") or []:
            normalized = str(tag).strip().lower()
            if normalized in tag_nodes:
                add_link(tag_nodes[normalized], node_id, "tagged", 1.1)

    file_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    session_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    tag_groups: dict[tuple[str, str], list[str]] = defaultdict(list)

    for record in memory_records:
        node_id = memory_node_ids[record["id"]]
        project = record["project"]
        for file_path in _normalized_files(record):
            file_groups[(project, file_path)].append(node_id)
        for session_ref in _normalized_session_refs(record):
            session_groups[(project, session_ref)].append(node_id)
        for tag in _normalized_tags(record):
            if tag in TAG_STOPWORDS:
                continue
            tag_groups[(project, tag)].append(node_id)

    def connect_grouped(nodes_for_group: list[str], kind: str, weight: float, max_pairs: int) -> None:
        pair_count = 0
        for index, source in enumerate(nodes_for_group):
            for target in nodes_for_group[index + 1:]:
                add_link(source, target, kind, weight)
                pair_count += 1
                if pair_count >= max_pairs:
                    return

    for (_, _), grouped_nodes in file_groups.items():
        if len(grouped_nodes) >= 2:
            connect_grouped(grouped_nodes[:6], "shared_file", 2.5, 10)

    for (_, _), grouped_nodes in session_groups.items():
        if len(grouped_nodes) >= 2:
            connect_grouped(grouped_nodes[:6], "shared_session", 2.0, 10)

    for (_, tag), grouped_nodes in tag_groups.items():
        if len(grouped_nodes) >= 2 and tag not in top_tags:
            connect_grouped(grouped_nodes[:5], "shared_tag", 1.15, 6)

    for slot in slots:
        node_id = slot["id"]
        add_node(
            {
                "id": node_id,
                "label": slot["label"],
                "type": "slot",
                "val": 8,
                "color": TYPE_COLORS["slot"],
                "meta": {
                    "project": slot["project"],
                    "domain": slot["domain"],
                    "description": slot["description"],
                    "content_preview": slot["content"][:320],
                    "size_limit": slot["size_limit"],
                },
            }
        )
        add_link(f"project::{slot['project']}", node_id, "holds_slot", 2.2)

        label_tokens = set(_slugify(slot["label"]).split("-"))
        for record in memory_records[:120]:
            if record["project"] != slot["project"]:
                continue
            content = (record.get("content") or "").lower()
            title = ((record.get("metadata") or {}).get("raw_title") or "").lower()
            if any(token and (token in content or token in title) for token in label_tokens):
                add_link(node_id, memory_node_ids[record["id"]], "slot_context", 1.35)

    if options.include_observations:
        for observation in observations:
            node_id = observation["id"]
            add_node(
                {
                    "id": node_id,
                    "label": observation["hook_type"],
                    "type": "observation",
                    "val": round(4.2 + min(observation["count"] / 4.0, 5.5), 2),
                    "color": TYPE_COLORS["observation"],
                    "meta": {
                        "project": observation["project"],
                        "hook_type": observation["hook_type"],
                        "domain": observation["domain"],
                        "sensitivity": observation["sensitivity"],
                        "restricted_count": observation["restricted_count"],
                        "sanitized_count": observation["sanitized_count"],
                        "observation_count": observation["count"],
                        "session_refs": observation["sample_session_ids"],
                        "cwd": ", ".join(observation["sample_cwds"]),
                    },
                }
            )
            add_link(f"project::{observation['project']}", node_id, "observed_in", 1.1)

    counts = Counter(node["type"] for node in nodes)
    return {
        "graph": {
            "nodes": nodes,
            "links": links,
        },
        "summary": {
            "project": options.project or "all-projects",
            "node_count": len(nodes),
            "link_count": len(links),
            "type_counts": dict(counts),
            "memory_records": len(memory_records),
            "slot_records": len(slots),
            "observation_records": len(observations) if options.include_observations else 0,
            "include_tags": options.include_tags,
            "include_observations": options.include_observations,
            "tag_limit": options.tag_limit,
            "limit": options.limit,
        },
    }


def camera_distance_for(node_count: int) -> int:
    return max(260, min(920, int(180 + math.sqrt(max(node_count, 1)) * 42)))
