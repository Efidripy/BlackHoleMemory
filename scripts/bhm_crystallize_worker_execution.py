from __future__ import annotations

from bhm_crystallize_worker_host import worker as _worker


def normalize_raw_item_for_file(
    file_name: str,
    item: _worker.JsonDict,
    max_record_chars: int,
) -> _worker.JsonDict | None:
    if file_name == _worker.MEMORIES_DB_FILE:
        return _worker.normalize_live_memory(item, max_record_chars)
    if file_name == _worker.OBSERVATIONS_DB_FILE:
        return _worker.normalize_observation(item, max_record_chars, source_file=file_name)
    return None


def raw_item_matches_batch_selector(
    file_name: str,
    item: _worker.JsonDict,
    batch: _worker.JsonDict,
    max_record_chars: int,
) -> bool:
    if not _worker.is_active(item):
        return False
    record = _worker.normalize_raw_item_for_file(file_name, item, max_record_chars)
    if record is None:
        return False
    if _worker.spam_reason(record):
        return False
    if _worker.safe_str(record.get("project")) != batch["project"]:
        return False
    if batch.get("group_kind") == "project" or batch.get("topic") == "mixed":
        return True
    return _worker.topic_for(record) == batch["topic"]


async def mark_archived_on_disk_async(
    runtime_dir: _worker.Path,
    batch: _worker.JsonDict,
    crystal_id: str,
    args: _worker.argparse.Namespace,
) -> _worker.JsonDict:
    now = _worker.utc_now()
    selected_by_file: dict[str, set[str]] = _worker.defaultdict(set)
    archive_records = batch.get("archive_records") or batch["records"]
    high_tier_selector = batch.get("tier") in {_worker.TIER_SINGULARITY, _worker.TIER_BIG_BANG}
    source_files = sorted({_worker.safe_str(record.get("source_file")) for record in archive_records if record.get("source_file")})
    if not high_tier_selector:
        for record in archive_records:
            selected_by_file[record["source_file"]].add(record["id"])

    result: _worker.JsonDict = {}
    for file_name in source_files:
        source_ids = selected_by_file.get(file_name, set())
        path = runtime_dir / file_name
        changed = 0

        if file_name == _worker.MEMORIES_DB_FILE:
            result[file_name] = {
                "changed": 0,
                "path": str(_worker.resolve_runtime_storage_config(runtime_dir=runtime_dir).database_path),
                "strategy": "sqlite-authoritative-api-only",
                "streamed": False,
                "replaced": False,
                "skipped": True,
                "read_only": True,
                "reason": "lifecycle archive is owned by BHM REST facade",
            }
            continue

        if file_name == _worker.OBSERVATIONS_DB_FILE:
            store = _worker.ObservationStore(path)
            if high_tier_selector:
                candidate_items = store.load(
                    project=batch["project"],
                    include_archived=False,
                )
                source_ids = {
                    _worker.safe_str(item.get("eventId") or item.get("id"))
                    for item in candidate_items
                    if _worker.raw_item_matches_batch_selector(file_name, item, batch, args.max_record_chars)
                }
            changed = store.archive(
                sorted(source_ids),
                archived_at=now,
                archive_reason=f"condensed into knowledge crystal {crystal_id}",
                condensed_into=crystal_id,
                archived_by=_worker.WORKER_NAME,
                scale_tier=_worker.safe_str(batch.get("tier") or _worker.TIER_MICRO),
            )
            result[file_name] = {
                "changed": changed,
                "path": str(path),
                "strategy": "sqlite-lifecycle-projection",
                "streamed": False,
                "replaced": False,
            }
            continue

        result[file_name] = {
            "changed": 0,
            "path": str(path),
            "strategy": "unsupported-source",
            "streamed": False,
            "replaced": False,
            "skipped": True,
            "reason": "only SQLite memory and observation sources are supported",
        }
    return result


async def apply_crystal(
    args: _worker.argparse.Namespace,
    batch: _worker.JsonDict,
    crystal_payload: _worker.JsonDict,
) -> _worker.JsonDict:
    await _worker.ensure_bhm_ready(args)
    project_hub_id = await _worker.get_or_rebuild_project_hub(args, batch["project"])
    refs = crystal_payload["metadata"].get("source_refs") or []
    consolidation = await _worker.consolidate_long_term_crystal(args, batch, crystal_payload)
    if consolidation.get("applied"):
        crystal_id = _worker.safe_str(consolidation.get("crystal_id"))
    else:
        crystal_id = await _worker.upsert_crystal(args, crystal_payload)
        await _worker.source_refs_attach(args, crystal_id, batch["project"], refs)
    links = _worker.link_items_for_batch(crystal_id, batch, project_hub_id)
    link_result = await _worker.create_links(args, links)
    live_archive = await _worker.archive_live_memory_sources(args, batch, crystal_id)
    disk_archive = await _worker.mark_archived_on_disk_async(args.runtime_dir, batch, crystal_id, args)
    return {
        "crystal_id": crystal_id,
        "project_hub_id": project_hub_id,
        "source_refs_attached": int(consolidation.get("source_refs_attached") or len(refs)),
        "consolidation": consolidation,
        "links_created": int(link_result.get("created_count") or 0),
        "link_result": link_result,
        "live_memories_archived": int(live_archive.get("archived_count") or 0),
        "live_archive": live_archive,
        "disk_archive": disk_archive,
    }


def build_report(
    args: _worker.argparse.Namespace,
    stats: _worker.JsonDict,
    batch: _worker.JsonDict | None,
    crystal_payload: _worker.JsonDict | None,
    crystal_id: str,
    project_hub_id: str | None,
    apply_result: _worker.JsonDict | None,
    soft_fail: str | None = None,
) -> _worker.JsonDict:
    records = batch["records"] if batch else []
    archive_records = (batch.get("archive_records") or records) if batch else []
    archive_targets = (batch.get("archive_targets") or [record.get("id") for record in archive_records]) if batch else []
    archive_target_strings = [_worker.safe_str(value) for value in archive_targets]
    live_archive_target_count = sum(1 for record in archive_records if record.get("source_kind") == "memory")
    return {
        "worker": _worker.WORKER_NAME,
        "mode": "apply" if args.apply else "dry-run",
        "ok": soft_fail is None and batch is not None and crystal_payload is not None,
        "soft_fail": soft_fail,
        "stats": stats,
        "harvest": {
            "project": batch["project"] if batch else None,
            "group_kind": batch["group_kind"] if batch else None,
            "group_key": batch["group_key"] if batch else None,
            "tier": batch.get("tier") if batch else None,
            "source_count": int(batch.get("total_pool_count") or len(records)) if batch else 0,
            "total_pool_count": int(batch.get("total_pool_count") or len(records)) if batch else 0,
            "prompt_sample_count": len(records),
            "prompt_source_ids": [record.get("id") for record in records],
            "archive_target_count": len(archive_target_strings),
            "archive_targets": _worker.report_list(archive_target_strings, args),
            "archive_targets_digest": _worker.stable_checksum("|".join(sorted(archive_target_strings))),
            "archive_targets_truncated": len(archive_target_strings) > _worker.report_limit(args),
            "source_breakdown": dict(_worker.Counter(record.get("source_file") for record in archive_records)),
            "context_zones": batch.get("context_zones") if batch else {},
        },
        "crystal": None
        if crystal_payload is None
        else {
            "id": crystal_id,
            "upsert_key": crystal_payload["upsert_key"],
            "type": crystal_payload["type"],
            "content": crystal_payload["content"],
            "metadata": _worker.report_metadata(crystal_payload["metadata"], args),
        },
        "consolidation": apply_result.get("consolidation") if apply_result else None,
        "archive": {
            "hard_delete": False,
            "planned_status": "archived",
            "tier_policy": batch.get("tier") if batch else None,
            "planned_target_count": len(archive_target_strings),
            "planned_live_memory_target_count": live_archive_target_count,
            "rest_chunk_size": _worker.BATCH_REST_CHUNK_SIZE,
            "bulk_filter_endpoint": _worker.BULK_ARCHIVE_ENDPOINT,
            "bulk_filter_attempted": False,
            "batch_archive_endpoint": "/bhm/memories/batch-archive",
            "fallback_method": "parallel_batch_archive_chunks"
            if batch and batch.get("tier") in {_worker.TIER_SINGULARITY, _worker.TIER_BIG_BANG}
            else "batch_archive_chunks",
            "applied": bool(args.apply and apply_result),
            "live_result": apply_result.get("live_archive") if apply_result else None,
            "result": apply_result.get("disk_archive") if apply_result else None,
        },
        "links": {
            "source_to_logs": "relation=condenses for live-memory sources; observation ids are stored as source_refs",
            "source_to_project_hub": "relation=belongs_to_project_hub",
            "project_hub_id": project_hub_id,
            "rest_chunk_size": _worker.BATCH_REST_CHUNK_SIZE,
            "result": apply_result.get("link_result") if apply_result else None,
            "created": apply_result.get("links_created") if apply_result else 0,
        },
    }
