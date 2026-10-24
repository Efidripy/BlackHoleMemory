from __future__ import annotations

from bhm_crystallize_worker_base import MAX_PAYLOAD_CHARS, trim_text
from bhm_crystallize_worker_host import worker as _worker


def build_frequency_map(batch: _worker.JsonDict, max_record_chars: int, include_example_content: bool = True) -> _worker.JsonDict:
    pool = batch.get("pool_records") or batch["records"]
    fingerprints: dict[str, list[_worker.JsonDict]] = _worker.defaultdict(list)
    for record in pool:
        fingerprints[_worker.failure_fingerprint(record)].append(record)

    ranked = sorted(
        fingerprints.items(),
        key=lambda item: (len(item[1]), max((_worker.record_time(record) for record in item[1]), default="")),
        reverse=True,
    )
    times = sorted(_worker.record_time(record) for record in pool if _worker.record_time(record))
    top_examples = []
    for index, record in enumerate(batch["records"][:_worker.SINGULARITY_EXAMPLE_LIMIT], start=1):
        example = {
            "n": index,
            "id": record["id"],
            "source_kind": record["source_kind"],
            "project": record["project"],
            "semantic_type": record["semantic_type"],
            "priority": record["priority"],
            "domain": record["domain"],
            "score": record.get("score"),
            "fingerprint": _worker.failure_fingerprint(record),
        }
        if include_example_content:
            example["content"] = trim_text(_worker.safe_str(record.get("content")), max_record_chars)
        top_examples.append(example)

    return {
        "kind": "frequency_map",
        "tier": _worker.TIER_SINGULARITY,
        "project": batch["project"],
        "topic": batch["topic"],
        "total_pool_count": batch["total_pool_count"],
        "timeframe": {
            "first": times[0] if times else "",
            "last": times[-1] if times else "",
        },
        "source_breakdown": dict(_worker.Counter(record.get("source_file") for record in pool)),
        "semantic_type_counts": dict(_worker.Counter(record.get("semantic_type") for record in pool)),
        "domain_counts": dict(_worker.Counter(record.get("domain") for record in pool)),
        "priority_counts": dict(_worker.Counter(record.get("priority") for record in pool)),
        "most_frequent_errors": [
            {
                "fingerprint": fingerprint,
                "count": len(records),
                "sample_ids": [record["id"] for record in records[:10]],
            }
            for fingerprint, records in ranked[:10]
        ],
        "top_examples": top_examples,
    }
def time_bounds(records: list[_worker.JsonDict]) -> _worker.JsonDict:
    first = ""
    last = ""
    for record in records:
        current = _worker.record_time(record)
        if not current:
            continue
        if not first or current < first:
            first = current
        if not last or current > last:
            last = current
    return {"first": first, "last": last}
def build_big_bang_passport(batch: _worker.JsonDict) -> _worker.JsonDict:
    pool = batch.get("pool_records") or batch.get("archive_records") or []
    signature_counts = _worker.Counter(
        _worker.safe_str(record.get("content_signature")) or _worker.stable_signature(_worker.safe_str(record.get("content")))
        for record in pool
    )
    dominant_signature, dominant_count = signature_counts.most_common(1)[0] if signature_counts else ("", 0)
    topic_hash = _worker.stable_checksum(f"{batch['project']}|{batch['topic']}|{dominant_signature}")
    total = int(batch.get("total_pool_count") or len(pool))
    return {
        "kind": "catastrophe_passport",
        "tier": _worker.TIER_BIG_BANG,
        "project": batch["project"],
        "topic": batch["topic"],
        "topic_hash": topic_hash,
        "dominant_error_hash": dominant_signature,
        "dominant_error_hash_count": dominant_count,
        "total_pool_count": total,
        "timeframe": time_bounds(pool),
        "source_breakdown": dict(_worker.Counter(record.get("source_file") for record in pool)),
        "prompt": (
            f"System storm detected: {total} logs in project={batch['project']} "
            f"topic={batch['topic']} hash={topic_hash}. Generate one Critical service warning."
        ),
    }
def tier_system_prompt(batch: _worker.JsonDict) -> str:
    tier = _worker.safe_str(batch.get("tier") or _worker.TIER_MICRO)
    total = int(batch.get("total_pool_count") or len(batch.get("records") or []))
    if tier == _worker.TIER_BIG_BANG:
        return (
            "Scale tier: BIG BANG / BLACK HOLE. Do not analyze raw logs. "
            f"A systemic storm of {total} logs was detected. Produce one Critical service warning "
            "from the catastrophe passport only.\n\n"
            f"{_worker.THREE_ZONE_CONTEXT_PROMPT}"
        )
    if tier == _worker.TIER_MACRO:
        return (
            f"{_worker.FACT_CRYSTAL_SYSTEM_PROMPT}\n\n"
            f"Scale tier: MACRO. You are compressing a macro-cluster of {total} logs. "
            "Identify the shared systemic anomaly, not individual incidents.\n\n"
            f"{_worker.THREE_ZONE_CONTEXT_PROMPT}"
        )
    if tier == _worker.TIER_SINGULARITY:
        return (
            f"{_worker.FACT_CRYSTAL_SYSTEM_PROMPT}\n\n"
            f"Scale tier: SINGULARITY FLOOD. You are analyzing an emergency flood of {total} logs. "
            "Use the provided Frequency Map as the source of truth. Do not infer from absent raw logs. "
            "Name the dominant failure signature, timeframe, blast radius, and emergency mitigation pattern.\n\n"
            f"{_worker.THREE_ZONE_CONTEXT_PROMPT}"
        )
    return f"{_worker.FACT_CRYSTAL_SYSTEM_PROMPT}\n\n{_worker.THREE_ZONE_CONTEXT_PROMPT}"
async def build_synthesis_payload(batch: _worker.JsonDict, args: _worker.argparse.Namespace) -> _worker.JsonDict:
    records = await _worker.build_three_zone_context(batch, args)
    active_zone = records.get("active_zone") if isinstance(records.get("active_zone"), dict) else {}
    compress_zone = records.get("compress_zone") if isinstance(records.get("compress_zone"), dict) else {}
    frozen_zone = records.get("frozen_zone") if isinstance(records.get("frozen_zone"), dict) else {}
    session_id = _worker.safe_str(
        batch.get("session_id")
        or batch.get("sessionId")
        or batch.get("group_key")
        or f"{batch['project']}::{batch['topic']}::{batch.get('tier') or _worker.TIER_MICRO}"
    )

    def zone_strings(values: _worker.Any) -> list[str]:
        if not isinstance(values, list):
            values = [values] if values else []
        output: list[str] = []
        for item in values:
            if isinstance(item, str):
                text = item
            else:
                text = _worker.safe_str(item)
            text = trim_text(text, args.max_record_chars)
            if text and text not in output:
                output.append(text)
        return output

    active_strings = zone_strings((active_zone.get("records") or []))
    compress_strings = zone_strings(compress_zone.get("entries") or [])
    if records.get("scale_context") is not None:
        compress_strings.append(trim_text(_worker.safe_str(records["scale_context"]), args.max_prompt_chars))
    frozen_strings = zone_strings(frozen_zone.get("distilled_crystallizer_conclusions") or [])
    return {
        "project_name": batch["project"],
        "session_id": session_id,
        "three_zone_context": {
            "Active": active_strings,
            "Compress": compress_strings,
            "Frozen": frozen_strings,
        },
    }

def synthesis_payload_chars(payload: _worker.JsonDict) -> int:
    return len(_worker.json.dumps(payload, ensure_ascii=False))


def synthesis_context(payload: _worker.JsonDict) -> _worker.JsonDict:
    context = payload.get("three_zone_context")
    return context if isinstance(context, dict) else {}


def synthesis_zone_items(payload: _worker.JsonDict, zone: str) -> list[str]:
    values = synthesis_context(payload).get(zone)
    if not isinstance(values, list):
        values = [values] if values else []
    output: list[str] = []
    for item in values:
        text = _worker.safe_str(item).strip()
        if text:
            output.append(text)
    return output


def synthesis_log_entries(payload: _worker.JsonDict) -> list[str]:
    entries: list[str] = []
    for zone in ("Active", "Compress"):
        for index, text in enumerate(synthesis_zone_items(payload, zone), start=1):
            entries.append(f"{zone} #{index}\n{text}")
    return entries


def clip_text(value: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(value) <= limit:
        return value
    return value[:limit]


def chunk_log_entries(logs: list[str], max_chars: int = MAX_PAYLOAD_CHARS) -> list[str]:
    chunks: list[str] = []
    current: list[str] = []
    current_chars = 0
    safe_limit = max(1, max_chars)

    for item in logs:
        text = _worker.safe_str(item).strip()
        if not text:
            continue
        if len(text) > safe_limit:
            text = clip_text(text, safe_limit)

        separator_chars = 2 if current else 0
        if current and current_chars + separator_chars + len(text) > safe_limit:
            chunks.append("\n\n".join(current))
            current = [text]
            current_chars = len(text)
        else:
            current.append(text)
            current_chars += separator_chars + len(text)

    if current:
        chunks.append("\n\n".join(current))
    return chunks


def stage_session_id(payload: _worker.JsonDict, suffix: str) -> str:
    session_id = _worker.safe_str(payload.get("session_id") or "fact-crystal-session")
    return f"{session_id}::{suffix}"


def base_stage_payload(payload: _worker.JsonDict, suffix: str) -> _worker.JsonDict:
    return {
        "project_name": _worker.safe_str(payload.get("project_name")),
        "session_id": stage_session_id(payload, suffix),
        "three_zone_context": {"Active": [], "Compress": [], "Frozen": []},
    }


def build_single_chunk_payload(payload: _worker.JsonDict, chunk_text: str) -> _worker.JsonDict:
    bounded = base_stage_payload(payload, "bounded-single")
    bounded["three_zone_context"]["Active"] = [chunk_text]
    return bounded


def build_map_stage_payload(payload: _worker.JsonDict, chunk_text: str, index: int, total: int) -> _worker.JsonDict:
    mapped = base_stage_payload(payload, f"map-{index:03d}-of-{total:03d}")
    mapped["three_zone_context"]["Active"] = [
        _worker.MAP_REDUCE_MAP_INSTRUCTION,
        f"Chunk {index}/{total}\n{chunk_text}",
    ]
    return mapped


def build_reduce_stage_payload(payload: _worker.JsonDict, summary_text: str, total: int) -> _worker.JsonDict:
    reduced = base_stage_payload(payload, f"reduce-{total:03d}")
    reduced["three_zone_context"]["Active"] = [_worker.MAP_REDUCE_REDUCE_INSTRUCTION]
    reduced["three_zone_context"]["Compress"] = [summary_text]
    return reduced


def available_stage_text_chars(payload: _worker.JsonDict) -> int:
    return max(500, _worker.MAX_PAYLOAD_CHARS - synthesis_payload_chars(payload) - 256)


def fit_stage_payload(payload: _worker.JsonDict, zone: str, index: int) -> _worker.JsonDict:
    context = synthesis_context(payload)
    values = context.get(zone)
    if not isinstance(values, list) or index >= len(values):
        return payload

    for _attempt in range(6):
        payload_chars = synthesis_payload_chars(payload)
        if payload_chars <= _worker.MAX_PAYLOAD_CHARS:
            return payload
        text = _worker.safe_str(values[index])
        excess = payload_chars - _worker.MAX_PAYLOAD_CHARS + 256
        limit = max(1, len(text) - excess)
        values[index] = clip_text(text, limit)
    return payload


def combine_intermediate_summaries(summaries: list[str], available_chars: int) -> str:
    if not summaries:
        return ""

    total = len(summaries)
    per_summary_limit = max(240, (max(available_chars, 1) // total) - 80)
    parts = [
        f"Intermediate summary {index}/{total}:\n{clip_text(summary, per_summary_limit)}"
        for index, summary in enumerate(summaries, start=1)
    ]
    combined = "\n\n".join(parts)
    if len(combined) > available_chars:
        combined = clip_text(combined, max(1, available_chars))
    return combined


async def ensure_bhm_ready(args: _worker.argparse.Namespace) -> _worker.JsonDict:
    ready = await _worker.async_rest_call(args.bhm_base_url, "GET", "/health/ready", None, args.timeout)
    if not bool(ready.get("ok", ready.get("required_ok", False))):
        raise _worker.SoftFail(f"BHM readiness failed: {_worker.json.dumps(ready, ensure_ascii=False)[:1200]}")
    return ready


def synthesis_fact_payload(response: _worker.JsonDict) -> _worker.JsonDict:
    fact = response.get("fact_crystal")
    if isinstance(fact, dict):
        return fact
    synthesis = response.get("synthesis")
    if isinstance(synthesis, dict) and isinstance(synthesis.get("fact_crystal"), dict):
        return synthesis["fact_crystal"]
    for key in ("summary", "content", "text", "result"):
        value = response.get(key)
        if isinstance(value, dict):
            return value
    return {}


def parse_synthesis_response(response: _worker.JsonDict) -> tuple[str, _worker.JsonDict]:
    fact_payload = synthesis_fact_payload(response)
    for key in ("crystal", "fact_crystal", "summary", "content", "text", "result"):
        value = response.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), fact_payload
        if isinstance(value, dict):
            return format_crystal_fields(value), value
    synthesis = response.get("synthesis")
    if isinstance(synthesis, dict):
        fact = synthesis.get("fact_crystal")
        if isinstance(fact, dict):
            return format_crystal_fields(fact), fact
    if isinstance(response.get("message"), dict):
        content = response["message"].get("content")
        if isinstance(content, str) and content.strip():
            return content.strip(), fact_payload
    raise _worker.SoftFail("BHM synthesis response did not contain crystal text")


def format_crystal_fields(fields: _worker.JsonDict) -> str:
    labels = [
        ("core_insight", "core_insight"),
        ("root_cause_resolved", "root_cause_resolved"),
        ("reusable_patterns", "reusable_patterns"),
        ("tags", "tags"),
        ("problem_or_solution_entity", "problem_or_solution_entity"),
        ("root_cause", "root_cause"),
        ("architecture_impact", "architecture_impact"),
        ("durable_guidance", "durable_guidance"),
        ("taxonomy_tags", "taxonomy_tags"),
    ]
    lines = ["Fact Crystal"]
    for key, label in labels:
        value = fields.get(key)
        if isinstance(value, list):
            value = ", ".join(_worker.safe_str(item) for item in value)
        value_text = _worker.normalize_space(_worker.safe_str(value))
        if value_text:
            lines.append(f"{label}: {value_text}")
    return "\n".join(lines)


async def call_synthesis_endpoint(payload: _worker.JsonDict, args: _worker.argparse.Namespace) -> tuple[str, _worker.JsonDict]:
    synthesis_endpoint = _worker.validate_synthesis_endpoint(args.synthesis_endpoint)
    payload_chars = synthesis_payload_chars(payload)
    if payload_chars > _worker.MAX_PAYLOAD_CHARS:
        raise _worker.SoftFail(f"BHM synthesis payload exceeds safe local LLM limit: {payload_chars} > {_worker.MAX_PAYLOAD_CHARS}")
    response = await _worker.async_rest_call(
        args.bhm_base_url,
        "POST",
        synthesis_endpoint,
        payload,
        args.timeout,
    )
    return parse_synthesis_response(response)


async def synthesize_with_map_reduce(
    payload: _worker.JsonDict,
    batch: _worker.JsonDict,
    args: _worker.argparse.Namespace,
    payload_chars: int,
) -> tuple[str, _worker.JsonDict]:
    entries = synthesis_log_entries(payload)
    map_template = build_map_stage_payload(payload, "", 1, 1)
    chunks = chunk_log_entries(entries, available_stage_text_chars(map_template))

    map_reduce_state = {
        "enabled": payload_chars > _worker.MAX_PAYLOAD_CHARS,
        "max_payload_chars": _worker.MAX_PAYLOAD_CHARS,
        "input_payload_chars": payload_chars,
        "chunks": len(chunks),
    }
    batch.setdefault("context_zones", {})["map_reduce"] = map_reduce_state

    if not chunks:
        raise _worker.SoftFail("BHM synthesis payload exceeded limit but no Active/Compress entries were available for chunking")

    if len(chunks) == 1:
        bounded_payload = fit_stage_payload(build_single_chunk_payload(payload, chunks[0]), "Active", 0)
        map_reduce_state["bounded_single_payload_chars"] = synthesis_payload_chars(bounded_payload)
        _worker.map_reduce_log(
            "Payload too large but Active/Compress context fits one bounded chunk. "
            f"Sending one normal synthesis request ({map_reduce_state['bounded_single_payload_chars']} chars)."
        )
        return await call_synthesis_endpoint(bounded_payload, args)

    total = len(chunks)
    _worker.map_reduce_log(f"Payload too large. Splitting into {total} chunks...")
    intermediate_summaries: list[str] = []
    max_map_payload_chars = 0

    for index, chunk in enumerate(chunks, start=1):
        chunk_payload = fit_stage_payload(build_map_stage_payload(payload, chunk, index, total), "Active", 1)
        chunk_payload_chars = synthesis_payload_chars(chunk_payload)
        max_map_payload_chars = max(max_map_payload_chars, chunk_payload_chars)
        _worker.map_reduce_log(f"Processing chunk {index}/{total} ({chunk_payload_chars} chars)...")
        summary_text, _summary_fact = await call_synthesis_endpoint(chunk_payload, args)
        intermediate_summaries.append(summary_text)

    reduce_template = build_reduce_stage_payload(payload, "", total)
    combined_summaries = combine_intermediate_summaries(
        intermediate_summaries,
        available_stage_text_chars(reduce_template),
    )
    reduce_payload = fit_stage_payload(build_reduce_stage_payload(payload, combined_summaries, total), "Compress", 0)
    reduce_payload_chars = synthesis_payload_chars(reduce_payload)
    map_reduce_state.update(
        {
            "intermediate_summaries": len(intermediate_summaries),
            "max_map_payload_chars": max_map_payload_chars,
            "reduce_payload_chars": reduce_payload_chars,
        }
    )
    _worker.map_reduce_log(
        f"Reducing {len(intermediate_summaries)} intermediate summaries "
        f"({reduce_payload_chars} chars)..."
    )
    return await call_synthesis_endpoint(reduce_payload, args)


async def synthesize_with_bhm(batch: _worker.JsonDict, args: _worker.argparse.Namespace) -> tuple[str, _worker.JsonDict]:
    if not args.synthesis_endpoint:
        raise _worker.SoftFail("BHM synthesis endpoint is disabled")
    payload = await _worker.build_synthesis_payload(batch, args)
    payload_chars = synthesis_payload_chars(payload)
    batch.setdefault("context_zones", {})["synthesis_payload_chars"] = payload_chars
    batch["context_zones"]["max_payload_chars"] = _worker.MAX_PAYLOAD_CHARS
    if payload_chars <= _worker.MAX_PAYLOAD_CHARS:
        return await call_synthesis_endpoint(payload, args)
    return await synthesize_with_map_reduce(payload, batch, args, payload_chars)


def deterministic_synthesis(batch: _worker.JsonDict) -> str:
    records = batch["records"]
    ids = [_worker.safe_str(record.get("id")) for record in records]
    tier = _worker.safe_str(batch.get("tier") or _worker.TIER_MICRO)
    total_count = int(batch.get("total_pool_count") or len(records))
    domains = _worker.Counter(_worker.safe_str(record.get("domain")) for record in records if record.get("domain"))
    semantic_types = _worker.Counter(_worker.safe_str(record.get("semantic_type")) for record in records if record.get("semantic_type"))
    concepts = _worker.Counter(
        concept
        for record in records
        for concept in record.get("concepts") or []
        if concept and not _worker.safe_str(concept).startswith("codex_")
    )
    dominant_domain = domains.most_common(1)[0][0] if domains else "infra"
    dominant_type = semantic_types.most_common(1)[0][0] if semantic_types else "log"
    tags = [item for item, _ in concepts.most_common(8)]
    if batch["topic"] not in tags:
        tags.insert(0, batch["topic"])
    if dominant_domain not in tags:
        tags.append(dominant_domain)
    if tier not in tags:
        tags.append(tier)

    if tier == _worker.TIER_BIG_BANG:
        passport = _worker.build_big_bang_passport(batch)
        timeframe = passport.get("timeframe") if isinstance(passport.get("timeframe"), dict) else {}
        entity = f"{batch['project']} BIG BANG log storm around {batch['topic']}"
        root_cause = (
            f"Systemic storm passport: {total_count} logs, topic_hash={_worker.safe_str(passport.get('topic_hash'))}, "
            f"dominant_error_hash={_worker.safe_str(passport.get('dominant_error_hash'))}. "
            f"Timeframe: {_worker.safe_str(timeframe.get('first'))}..{_worker.safe_str(timeframe.get('last'))}."
        )
    elif tier == _worker.TIER_SINGULARITY:
        frequency_map = _worker.build_frequency_map(batch, _worker.DEFAULT_MAX_RECORD_CHARS)
        dominant = (frequency_map.get("most_frequent_errors") or [{}])[0]
        fingerprint = _worker.safe_str(dominant.get("fingerprint") if isinstance(dominant, dict) else "")
        count = int(dominant.get("count") or 0) if isinstance(dominant, dict) else 0
        timeframe = frequency_map.get("timeframe") if isinstance(frequency_map.get("timeframe"), dict) else {}
        entity = f"{batch['project']} singularity flood around {batch['topic']}"
        root_cause = (
            f"Dominant failure signature repeated {count} of {total_count} times: {fingerprint}. "
            f"Timeframe: {_worker.safe_str(timeframe.get('first'))}..{_worker.safe_str(timeframe.get('last'))}."
        )
    elif tier == _worker.TIER_MACRO:
        entity = f"{batch['project']} macro anomaly around {batch['topic']}"
        root_cause = "A 100+ record operational cluster points to one shared systemic anomaly."
    elif dominant_type == "error":
        entity = f"{batch['project']} failure pattern around {batch['topic']}"
        root_cause = "Repeated operational errors or failed tool/runtime calls share the same semantic cluster."
    elif "decision" in tags or batch["topic"] == "decision":
        entity = f"{batch['project']} architectural decision cluster around {batch['topic']}"
        root_cause = "Multiple operational records point to a durable decision that should be remembered as a fact."
    else:
        entity = f"{batch['project']} operational pattern around {batch['topic']}"
        root_cause = "Raw telemetry repeated enough to justify compression into a long-term memory fact."

    return "\n".join(
        [
            "Fact Crystal",
            f"problem_or_solution_entity: {entity}",
            f"root_cause: {root_cause}",
            f"architecture_impact: Active retrieval should use this crystal instead of replaying {total_count} raw source records.",
            "durable_guidance: Keep raw sources archived on disk, retain source IDs in metadata, and link live-memory sources with relation=condenses.",
            f"taxonomy_tags: {', '.join(tags[:10])}",
            f"domain: {dominant_domain if dominant_domain in _worker.VALID_DOMAINS else 'general'}",
            f"priority: {'critical' if tier in {_worker.TIER_SINGULARITY, _worker.TIER_BIG_BANG} else 'high'}",
            f"semantic_type: {'bugfix' if dominant_type == 'error' else 'knowledge'}",
            f"source_sample_ids: {', '.join(ids)}",
        ]
    )


async def synthesize_crystal(batch: _worker.JsonDict, args: _worker.argparse.Namespace) -> tuple[str, str, _worker.JsonDict]:
    try:
        crystal_text, synthesis_fact = await synthesize_with_bhm(batch, args)
        return crystal_text, "bhm-rest", synthesis_fact
    except _worker.SoftFail as exc:
        if args.apply and not args.allow_fallback_synthesis:
            raise
        _worker.log(f"synthesis endpoint unavailable, using deterministic fallback: {exc}")
        return deterministic_synthesis(batch), "deterministic-fallback", {}


