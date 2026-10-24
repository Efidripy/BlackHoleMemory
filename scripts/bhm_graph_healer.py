#!/usr/bin/env python
"""Retroactively stitch orphaned BHM Qdrant points into semantic_graph.json.

Safety contract:
- dry-run is the default mode;
- Qdrant points are read-only;
- durable graph writes happen only through BHMGraphManager.add_semantic_link;
- existing semantic links and aliases are respected to avoid duplicate stitching.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Iterable
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mem0_adapter import BHMGraphManager  # noqa: E402
from blackholememory.mem0_adapter import GLOBAL_COLLECTION_NAME  # noqa: E402
from blackholememory.mem0_adapter import LOCAL_COLLECTION_PREFIX  # noqa: E402
from blackholememory.mem0_adapter import get_qdrant_client  # noqa: E402


DEFAULT_TOP_K = 3
DEFAULT_SCORE_THRESHOLD = 0.85
DEFAULT_SCROLL_BATCH_SIZE = 256
DEFAULT_RATE_LIMIT_MS = 25
DEFAULT_REPORT_LIMIT = 50
RUNTIME_RELEASE = "v4.5.0-PURE-HEALER"
SMOKE_COLLECTION_MARKERS = ("_smoke", "smoke_")

JsonDict = dict[str, Any]


@dataclass(frozen=True)
class HealerPoint:
    collection_name: str
    point_id: str
    node_id: str
    aliases: tuple[str, ...]
    payload: JsonDict
    vector: Any
    vector_name: str | None = None


@dataclass(frozen=True)
class NeighborHit:
    point: HealerPoint
    score: float


@dataclass(frozen=True)
class LinkPlan:
    source_id: str
    target_id: str
    edge_type: str
    score: float
    collection_name: str
    source_point_id: str
    target_point_id: str
    source_last_accessed_at: str | None
    target_last_accessed_at: str | None


@dataclass
class HealerSummary:
    ok: bool
    mode: str
    runtime_release: str = RUNTIME_RELEASE
    collections: list[str] | None = None
    scanned_points: int = 0
    skipped_missing_vector: int = 0
    skipped_smoke_points: int = 0
    orphans_found: int = 0
    orphans_processed: int = 0
    planned_links: int = 0
    applied_links: int = 0
    top_k: int = DEFAULT_TOP_K
    score_threshold: float = DEFAULT_SCORE_THRESHOLD
    errors: list[str] | None = None
    plans: list[JsonDict] | None = None

    def to_dict(self) -> JsonDict:
        payload = asdict(self)
        payload["collections"] = self.collections or []
        payload["errors"] = self.errors or []
        payload["plans"] = self.plans or []
        return payload


def _string_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, Iterable):
        return [text for item in value if (text := _string_or_none(item))]
    text = _string_or_none(value)
    return [text] if text else []


def _nested_metadata(payload: JsonDict) -> JsonDict:
    metadata = payload.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def _payload_value(payload: JsonDict, key: str) -> Any:
    metadata = _nested_metadata(payload)
    return payload.get(key) if payload.get(key) is not None else metadata.get(key)


def is_smoke_collection_name(collection_name: str) -> bool:
    return any(marker in collection_name for marker in SMOKE_COLLECTION_MARKERS)


def is_smoke_payload(payload: JsonDict) -> bool:
    for key in ("project", "project_name", "upsert_key", "origin", "source_id"):
        value = _payload_value(payload, key)
        if "smoke" in str(value or "").lower():
            return True
    return False


def _node_id_for_point(point_id: str, payload: JsonDict) -> str:
    return (
        _string_or_none(_payload_value(payload, "source_id"))
        or _string_or_none(_payload_value(payload, "id"))
        or point_id
    )


def point_aliases(point_id: str, payload: JsonDict) -> tuple[str, ...]:
    aliases: list[str] = [
        point_id,
        _payload_value(payload, "source_id"),
        _payload_value(payload, "id"),
        _payload_value(payload, "mem0_hit_id"),
    ]
    aliases.extend(_string_list(_payload_value(payload, "mem0_ids")))
    aliases.extend(_string_list(_payload_value(payload, "global_mem0_ids")))
    return tuple(dict.fromkeys(text for item in aliases if (text := _string_or_none(item))))


def graph_mentioned_ids(graph: dict[str, list[dict[str, str]]]) -> set[str]:
    mentioned: set[str] = set()
    for source_id, links in (graph or {}).items():
        if source := _string_or_none(source_id):
            mentioned.add(source)
        for link in links or []:
            if not isinstance(link, dict):
                continue
            if target := _string_or_none(link.get("target_id")):
                mentioned.add(target)
    return mentioned


def graph_edge_keys(graph: dict[str, list[dict[str, str]]]) -> set[tuple[str, str, str]]:
    keys: set[tuple[str, str, str]] = set()
    for source_id, links in (graph or {}).items():
        source = _string_or_none(source_id)
        if not source:
            continue
        for link in links or []:
            if not isinstance(link, dict):
                continue
            target = _string_or_none(link.get("target_id"))
            edge_type = _string_or_none(link.get("edge_type"))
            if target and edge_type:
                keys.add((source, target, edge_type.upper()))
    return keys


def discover_orphans(points: list[HealerPoint], graph: dict[str, list[dict[str, str]]]) -> list[HealerPoint]:
    mentioned = graph_mentioned_ids(graph)
    return [point for point in points if not mentioned.intersection(point.aliases)]


def _parse_timestamp(value: Any) -> datetime | None:
    text = _string_or_none(value)
    if not text:
        return None
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def point_timestamp(point: HealerPoint) -> datetime:
    payload = point.payload
    for key in ("last_accessed_at", "updated_at", "created_at", "backfilled_at"):
        if parsed := _parse_timestamp(_payload_value(payload, key)):
            return parsed
    return datetime.fromtimestamp(0, tz=timezone.utc)


def point_timestamp_text(point: HealerPoint) -> str | None:
    payload = point.payload
    for key in ("last_accessed_at", "updated_at", "created_at", "backfilled_at"):
        if text := _string_or_none(_payload_value(payload, key)):
            return text
    return None


def classify_edge_by_time(source: HealerPoint, neighbor: HealerPoint) -> str:
    if point_timestamp(neighbor) > point_timestamp(source):
        return "UPGRADES"
    return "DEPENDS_ON"


def _extract_vector(point: Any) -> tuple[Any, str | None]:
    vector = getattr(point, "vector", None)
    if vector is None and isinstance(point, dict):
        vector = point.get("vector")
    if isinstance(vector, dict):
        for name, value in vector.items():
            if value is not None:
                return value, str(name)
        return None, None
    return vector, None


def point_from_qdrant(collection_name: str, point: Any) -> HealerPoint | None:
    point_id = _string_or_none(getattr(point, "id", None))
    if point_id is None and isinstance(point, dict):
        point_id = _string_or_none(point.get("id"))
    if not point_id:
        return None

    payload = getattr(point, "payload", None)
    if payload is None and isinstance(point, dict):
        payload = point.get("payload")
    payload = dict(payload or {})

    vector, vector_name = _extract_vector(point)
    if vector is None:
        return None

    aliases = point_aliases(point_id, payload)
    return HealerPoint(
        collection_name=collection_name,
        point_id=point_id,
        node_id=_node_id_for_point(point_id, payload),
        aliases=aliases,
        payload=payload,
        vector=vector,
        vector_name=vector_name,
    )


def _collection_response_names(response: Any) -> list[str]:
    collections = getattr(response, "collections", response)
    names: list[str] = []
    for item in collections or []:
        if isinstance(item, str):
            name = item
        elif isinstance(item, dict):
            name = str(item.get("name") or "")
        else:
            name = str(getattr(item, "name", "") or "")
        if name:
            names.append(name)
    return names


def memory_collection_names(client: Any, *, include_smoke: bool = False) -> list[str]:
    names = _collection_response_names(client.get_collections())
    return sorted(
        name
        for name in names
        if name == GLOBAL_COLLECTION_NAME or name.startswith(f"{LOCAL_COLLECTION_PREFIX}_")
        if include_smoke or not is_smoke_collection_name(name)
    )


async def _scroll_page(
    client: Any,
    *,
    collection_name: str,
    limit: int,
    offset: Any,
) -> tuple[list[Any], Any]:
    def run_scroll() -> tuple[list[Any], Any]:
        try:
            return client.scroll(
                collection_name=collection_name,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=True,
                scroll_filter=None,
            )
        except TypeError:
            return client.scroll(
                collection_name=collection_name,
                limit=limit,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )

    return await asyncio.to_thread(run_scroll)


async def scroll_memory_points(
    client: Any,
    *,
    collection_names: list[str],
    batch_size: int = DEFAULT_SCROLL_BATCH_SIZE,
    max_points: int | None = None,
    include_smoke: bool = False,
) -> tuple[list[HealerPoint], int, int]:
    points: list[HealerPoint] = []
    skipped_missing_vector = 0
    skipped_smoke_points = 0

    for collection_name in collection_names:
        offset = None
        while True:
            remaining = None if max_points is None else max(max_points - len(points), 0)
            if remaining == 0:
                return points, skipped_missing_vector, skipped_smoke_points
            page_limit = min(batch_size, remaining) if remaining is not None else batch_size
            records, offset = await _scroll_page(
                client,
                collection_name=collection_name,
                limit=page_limit,
                offset=offset,
            )
            if not records:
                break
            for raw_point in records:
                point = point_from_qdrant(collection_name, raw_point)
                if point is None:
                    skipped_missing_vector += 1
                    continue
                if not include_smoke and is_smoke_payload(point.payload):
                    skipped_smoke_points += 1
                    continue
                points.append(point)
            if offset is None:
                break

    return points, skipped_missing_vector, skipped_smoke_points


def index_points_by_alias(points: list[HealerPoint]) -> dict[str, HealerPoint]:
    index: dict[str, HealerPoint] = {}
    for point in points:
        for alias in point.aliases:
            index.setdefault(alias, point)
    return index


def _hit_id(hit: Any) -> str | None:
    if isinstance(hit, dict):
        return _string_or_none(hit.get("id"))
    return _string_or_none(getattr(hit, "id", None))


def _hit_score(hit: Any) -> float:
    value = hit.get("score") if isinstance(hit, dict) else getattr(hit, "score", 0.0)
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _hit_payload(hit: Any) -> JsonDict:
    payload = hit.get("payload") if isinstance(hit, dict) else getattr(hit, "payload", None)
    return dict(payload or {})


def _query_response_points(response: Any) -> list[Any]:
    if isinstance(response, list):
        return response
    points = getattr(response, "points", None)
    if points is not None:
        return list(points)
    result = getattr(response, "result", None)
    if result is not None:
        return list(result)
    if isinstance(response, dict):
        for key in ("points", "result", "hits"):
            if isinstance(response.get(key), list):
                return response[key]
    return []


async def search_similar_points(
    client: Any,
    *,
    point: HealerPoint,
    top_k: int,
    score_threshold: float,
    points_by_alias: dict[str, HealerPoint],
) -> list[NeighborHit]:
    def run_search() -> list[Any]:
        if hasattr(client, "search"):
            return client.search(
                collection_name=point.collection_name,
                query_vector=point.vector,
                limit=top_k,
                score_threshold=score_threshold,
                with_payload=True,
                with_vectors=False,
            )
        response = client.query_points(
            collection_name=point.collection_name,
            query=point.vector,
            using=point.vector_name,
            limit=top_k,
            score_threshold=score_threshold,
            with_payload=True,
            with_vectors=False,
        )
        return _query_response_points(response)

    hits = await asyncio.to_thread(run_search)
    neighbors: list[NeighborHit] = []
    source_aliases = set(point.aliases)
    seen_targets: set[str] = set()

    for hit in hits:
        score = _hit_score(hit)
        if score <= score_threshold:
            continue
        hit_id = _hit_id(hit)
        payload = _hit_payload(hit)
        hit_aliases = point_aliases(hit_id or "", payload) if hit_id else tuple()
        if hit_id and hit_id in points_by_alias:
            neighbor = points_by_alias[hit_id]
        else:
            candidate = {"id": hit_id, "payload": payload, "vector": []}
            neighbor = point_from_qdrant(point.collection_name, candidate)
        if neighbor is None:
            continue
        if source_aliases.intersection(neighbor.aliases or hit_aliases):
            continue
        if neighbor.node_id in seen_targets:
            continue
        seen_targets.add(neighbor.node_id)
        neighbors.append(NeighborHit(point=neighbor, score=score))

    return neighbors


def plan_links_for_orphan(
    orphan: HealerPoint,
    neighbors: list[NeighborHit],
    *,
    existing_edges: set[tuple[str, str, str]],
) -> list[LinkPlan]:
    plans: list[LinkPlan] = []
    planned_edges = set(existing_edges)
    for neighbor in neighbors:
        target = neighbor.point
        if orphan.node_id == target.node_id:
            continue
        edge_type = classify_edge_by_time(orphan, target)
        key = (orphan.node_id, target.node_id, edge_type)
        if key in planned_edges:
            continue
        planned_edges.add(key)
        plans.append(
            LinkPlan(
                source_id=orphan.node_id,
                target_id=target.node_id,
                edge_type=edge_type,
                score=neighbor.score,
                collection_name=orphan.collection_name,
                source_point_id=orphan.point_id,
                target_point_id=target.point_id,
                source_last_accessed_at=point_timestamp_text(orphan),
                target_last_accessed_at=point_timestamp_text(target),
            )
        )
    return plans


async def heal_graph(
    *,
    dry_run: bool = True,
    client: Any | None = None,
    graph_manager: BHMGraphManager | None = None,
    collection_names: list[str] | None = None,
    top_k: int = DEFAULT_TOP_K,
    score_threshold: float = DEFAULT_SCORE_THRESHOLD,
    batch_size: int = DEFAULT_SCROLL_BATCH_SIZE,
    max_points: int | None = None,
    max_orphans: int | None = None,
    max_links: int | None = None,
    rate_limit_ms: int = DEFAULT_RATE_LIMIT_MS,
    report_limit: int = DEFAULT_REPORT_LIMIT,
    include_smoke_collections: bool = False,
) -> HealerSummary:
    mode = "DRY-RUN" if dry_run else "APPLY"
    client = client or get_qdrant_client()
    graph_manager = graph_manager or BHMGraphManager()
    errors: list[str] = []
    applied_links = 0

    graph = await graph_manager.get_graph()
    allowed_collections = set(memory_collection_names(client, include_smoke=include_smoke_collections))
    if collection_names:
        requested_collections = list(dict.fromkeys(str(name).strip() for name in collection_names if str(name).strip()))
        invalid_collections = sorted(set(requested_collections) - allowed_collections)
        if invalid_collections:
            raise ValueError(f"collection_not_allowed: {', '.join(invalid_collections)}")
        collections = requested_collections
    else:
        collections = sorted(allowed_collections)
    points, skipped_missing_vector, skipped_smoke_points = await scroll_memory_points(
        client,
        collection_names=collections,
        batch_size=batch_size,
        max_points=max_points,
        include_smoke=include_smoke_collections,
    )
    orphans = discover_orphans(points, graph)
    points_by_alias = index_points_by_alias(points)
    existing_edges = graph_edge_keys(graph)
    all_plans: list[LinkPlan] = []
    processed_orphans = 0

    for orphan in orphans:
        if max_orphans is not None and processed_orphans >= max_orphans:
            break
        processed_orphans += 1
        try:
            neighbors = await search_similar_points(
                client,
                point=orphan,
                top_k=top_k,
                score_threshold=score_threshold,
                points_by_alias=points_by_alias,
            )
            plans = plan_links_for_orphan(orphan, neighbors, existing_edges=existing_edges)
        except Exception as exc:
            errors.append(f"{orphan.collection_name}:{orphan.point_id}: {type(exc).__name__}: {exc}")
            plans = []

        for plan in plans:
            if max_links is not None and len(all_plans) >= max_links:
                break
            key = (plan.source_id, plan.target_id, plan.edge_type)
            if key in existing_edges:
                continue
            existing_edges.add(key)
            all_plans.append(plan)
            if not dry_run:
                try:
                    await graph_manager.add_semantic_link(plan.source_id, plan.target_id, plan.edge_type)
                    applied_links += 1
                except Exception as exc:
                    errors.append(f"{plan.source_id}->{plan.target_id}: {type(exc).__name__}: {exc}")
        if max_links is not None and len(all_plans) >= max_links:
            break
        if rate_limit_ms > 0:
            await asyncio.sleep(rate_limit_ms / 1000.0)

    plan_dicts = [asdict(plan) for plan in all_plans[: max(report_limit, 0)]]
    return HealerSummary(
        ok=not errors,
        mode=mode,
        collections=collections,
        scanned_points=len(points),
        skipped_missing_vector=skipped_missing_vector,
        skipped_smoke_points=skipped_smoke_points,
        orphans_found=len(orphans),
        orphans_processed=processed_orphans,
        planned_links=len(all_plans),
        applied_links=applied_links,
        top_k=top_k,
        score_threshold=score_threshold,
        errors=errors,
        plans=plan_dicts,
    )


def _positive_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _non_negative_int_arg(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("value must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be non-negative")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Heal orphaned BHM semantic graph nodes from Qdrant KNN neighbors.")
    parser.add_argument("--apply", action="store_true", default=False, help="Write planned links to semantic_graph.json.")
    parser.add_argument("--dry-run", action="store_true", default=False, help="Force read-only planning mode.")
    parser.add_argument("--top-k", type=_positive_int_arg, default=DEFAULT_TOP_K, help="KNN result limit; default: 3.")
    parser.add_argument("--threshold", type=float, default=DEFAULT_SCORE_THRESHOLD, help="Strict score cutoff; default: 0.85.")
    parser.add_argument("--batch-size", type=_positive_int_arg, default=DEFAULT_SCROLL_BATCH_SIZE, help="Qdrant scroll page size.")
    parser.add_argument("--max-points", type=_positive_int_arg, default=None, help="Optional scanned point cap.")
    parser.add_argument("--max-orphans", type=_positive_int_arg, default=None, help="Optional orphan processing cap.")
    parser.add_argument("--max-links", type=_positive_int_arg, default=None, help="Optional link planning/apply cap.")
    parser.add_argument("--rate-limit-ms", type=_non_negative_int_arg, default=DEFAULT_RATE_LIMIT_MS, help="Delay between orphan KNN calls.")
    parser.add_argument("--report-limit", type=_non_negative_int_arg, default=DEFAULT_REPORT_LIMIT, help="Maximum plans included in console summary.")
    parser.add_argument("--collection", action="append", dest="collections", help="Explicit collection to scan; repeatable.")
    parser.add_argument("--include-smoke-collections", action="store_true", default=False, help="Include temporary smoke/test local collections.")
    parser.add_argument("--json", action="store_true", default=False, help="Print machine-readable summary JSON.")
    return parser


def print_human_summary(summary: HealerSummary) -> None:
    data = summary.to_dict()
    print(f"[{summary.mode}] BHM Graph Healer {summary.runtime_release}", flush=True)
    print(f"Collections: {', '.join(data['collections']) or '(none)'}", flush=True)
    print(
        "Scanned points: {scanned_points}. Orphans found: {orphans_found}. "
        "Planned links: {planned_links}. Applied links: {applied_links}.".format(**data),
        flush=True,
    )
    if summary.skipped_missing_vector:
        print(f"Skipped points without vectors: {summary.skipped_missing_vector}", flush=True)
    if summary.skipped_smoke_points:
        print(f"Skipped smoke/test payloads: {summary.skipped_smoke_points}", flush=True)
    for plan in data["plans"]:
        print(
            "[PLAN] {source_id} --{edge_type}/{score:.3f}--> {target_id} "
            "({collection_name}; {source_point_id} -> {target_point_id})".format(**plan),
            flush=True,
        )
    hidden = summary.planned_links - len(data["plans"])
    if hidden > 0:
        print(f"[INFO] {hidden} additional planned links omitted by --report-limit.", flush=True)
    for error in data["errors"]:
        print(f"[ERROR] {error}", flush=True)


async def async_main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    dry_run = not args.apply or args.dry_run
    summary = await heal_graph(
        dry_run=dry_run,
        collection_names=args.collections,
        top_k=args.top_k,
        score_threshold=args.threshold,
        batch_size=args.batch_size,
        max_points=args.max_points,
        max_orphans=args.max_orphans,
        max_links=args.max_links,
        rate_limit_ms=args.rate_limit_ms,
        report_limit=args.report_limit,
        include_smoke_collections=args.include_smoke_collections,
    )
    if args.json:
        print(json.dumps(summary.to_dict(), ensure_ascii=False, indent=2), flush=True)
    else:
        print_human_summary(summary)
    return 0 if summary.ok else 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(async_main(argv))


if __name__ == "__main__":
    raise SystemExit(main())
