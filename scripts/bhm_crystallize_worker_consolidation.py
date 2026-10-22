from __future__ import annotations

from bhm_crystallize_worker_host import worker as _worker


def majority(records: list[_worker.JsonDict], key: str, default: str, allowed: set[str] | None = None) -> str:
    values = [_worker.safe_str(record.get(key)).strip().lower() for record in records if record.get(key)]
    if not values:
        return default
    selected = _worker.Counter(values).most_common(1)[0][0]
    if allowed is not None and selected not in allowed:
        return default
    return selected


def source_ref(record: _worker.JsonDict) -> str:
    return f"{record['source_file']}::{record['id']}"


def chunked(items: list[_worker.JsonDict], size: int):
    for index in range(0, len(items), size):
        yield items[index : index + size]


def report_limit(args: _worker.argparse.Namespace) -> int:
    return args.debug_limit if args.debug_limit > 0 else _worker.DEFAULT_REPORT_LIST_LIMIT


def report_list(values: list[_worker.Any], args: _worker.argparse.Namespace) -> list[_worker.Any]:
    limit = report_limit(args)
    if len(values) <= limit:
        return values
    return values[:limit] + [f"... truncated {len(values) - limit} more items"]


def report_metadata(metadata: _worker.JsonDict, args: _worker.argparse.Namespace) -> _worker.JsonDict:
    report: _worker.JsonDict = {}
    for key, value in metadata.items():
        if isinstance(value, list):
            report[key] = report_list(value, args)
            if len(value) > report_limit(args):
                report[f"{key}_count"] = len(value)
                report[f"{key}_truncated"] = True
            continue
        report[key] = value
    return report


def unique_strings(values: list[_worker.Any], limit: int | None = None) -> list[str]:
    unique = [_worker.safe_str(value).strip() for value in values if _worker.safe_str(value).strip()]
    unique = list(dict.fromkeys(unique))
    return unique[:limit] if limit is not None else unique


def token_set(value: _worker.Any) -> set[str]:
    text = _worker.safe_str(value).lower()
    tokens = _worker.re.findall(r"[a-zа-яё0-9_.:/\\-]{3,}", text, _worker.re.IGNORECASE)
    return {
        token.strip("._:/\\-")
        for token in tokens
        if token.strip("._:/\\-") and token not in _worker.CONSOLIDATION_TOKEN_STOPWORDS and not token.isdigit()
    }


def jaccard(left: set[str], right: set[str]) -> float:
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def list_from_value(value: _worker.Any) -> list[str]:
    if isinstance(value, list):
        return unique_strings(value)
    if isinstance(value, tuple):
        return unique_strings(list(value))
    if isinstance(value, str):
        return unique_strings([part.strip() for part in _worker.re.split(r"[,;\n]", value)])
    return []


def normalize_candidate_memory(item: _worker.JsonDict) -> _worker.JsonDict | None:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    candidate_id = _worker.safe_str(
        item.get("id")
        or item.get("source_id")
        or metadata.get("source_id")
        or metadata.get("id")
    ).strip()
    if not candidate_id:
        return None
    content = _worker.safe_str(item.get("content") or item.get("memory"))
    concepts = list_from_value(item.get("concepts") or item.get("tags") or metadata.get("tags") or [])
    files = list_from_value(item.get("files") or metadata.get("files") or [])
    memory_type = _worker.safe_str(item.get("type") or item.get("memory_type") or metadata.get("memory_type")).strip()
    project = _worker.safe_str(item.get("project") or metadata.get("project")).strip()
    return {
        "id": candidate_id,
        "project": project,
        "type": memory_type,
        "content": content,
        "concepts": concepts,
        "files": files,
        "upsert_key": _worker.safe_str(item.get("upsert_key") or metadata.get("upsert_key")),
        "created_at": _worker.safe_str(item.get("created_at") or metadata.get("created_at")),
        "updated_at": _worker.safe_str(item.get("updated_at") or metadata.get("updated_at")),
        "metadata": metadata,
    }


def is_active_long_term_fact_crystal(candidate: _worker.JsonDict, project: str) -> bool:
    metadata = candidate.get("metadata") or {}
    lifecycle = _worker.safe_str(metadata.get("lifecycle")).strip().lower()
    if metadata.get("archived_at") or lifecycle in _worker.ARCHIVED_STATES:
        return False
    if project and candidate.get("project") and candidate["project"] != project:
        return False
    memory_type = _worker.safe_str(candidate.get("type")).strip().lower()
    concepts = {concept.lower() for concept in candidate.get("concepts") or []}
    if memory_type != "knowledge-crystal" and "knowledge-crystal" not in concepts and "fact-crystal" not in concepts:
        return False
    semantic_type = _worker.safe_str(metadata.get("semantic_type")).strip().lower()
    return semantic_type in (_worker.CRYSTAL_SEMANTIC_TYPES | {"fact"}) and _worker.safe_str(metadata.get("retention")).strip().lower() == "long-term"


def consolidation_query(batch: _worker.JsonDict, crystal_payload: _worker.JsonDict) -> str:
    parts = [
        batch["project"],
        batch["topic"],
        _worker.safe_str(crystal_payload["metadata"].get("problem_entity")),
        " ".join(crystal_payload.get("concepts") or []),
        " ".join(crystal_payload.get("files") or []),
        _worker.safe_str(crystal_payload.get("content"), 900),
    ]
    return _worker.normalize_space(" ".join(part for part in parts if part))[:1400]


async def collect_consolidation_candidates(
    args: _worker.argparse.Namespace,
    batch: _worker.JsonDict,
    crystal_payload: _worker.JsonDict,
) -> list[_worker.JsonDict]:
    project = batch["project"]
    query = consolidation_query(batch, crystal_payload)
    domain = crystal_payload["metadata"].get("domain")
    raw_candidates: list[_worker.JsonDict] = []

    search_calls = [
        (
            "POST",
            "/mem0/search",
            {
                "query": query,
                "project": project,
                "top_k": min(_worker.CONSOLIDATION_SEARCH_LIMIT, 50),
                "domain": domain,
                "include_archived": False,
                "include_logs": False,
            },
            None,
        ),
        (
            "POST",
            "/bhm/search/advanced",
            {
                "query": query,
                "project": project,
                "memory_type": "knowledge-crystal",
                "include_archived": False,
                "include_logs": False,
                "limit": _worker.CONSOLIDATION_SEARCH_LIMIT,
            },
            None,
        ),
        (
            "GET",
            "/bhm/memories",
            None,
            {
                "project": project,
                "memory_type": "knowledge-crystal",
                "include_archived": False,
                "limit": 200,
            },
        ),
    ]

    async def run_search(method: str, path: str, payload: _worker.JsonDict | None, params: _worker.JsonDict | None):
        try:
            result = await _worker.async_rest_call(args.bhm_base_url, method, path, payload, args.timeout, params)
            return path, result, None
        except _worker.SoftFail as exc:
            return path, None, exc

    search_results = await _worker.asyncio.gather(*(run_search(method, path, payload, params) for method, path, payload, params in search_calls))
    for path, result, failure in search_results:
        if failure is not None:
            _worker.log(f"consolidation search skipped for {path}: {failure}")
            continue
        if not isinstance(result, dict):
            continue
        if path == "/mem0/search":
            result_items = ((result.get("result") or {}).get("results") or [])
        else:
            result_items = result.get("memories") or []
        raw_candidates.extend(item for item in result_items if isinstance(item, dict))

    candidates: dict[str, _worker.JsonDict] = {}
    for item in raw_candidates:
        candidate = normalize_candidate_memory(item)
        if candidate is None:
            continue
        if candidate["upsert_key"] == crystal_payload.get("upsert_key"):
            if is_active_long_term_fact_crystal(candidate, project):
                candidates[candidate["id"]] = candidate
            continue
        if not is_active_long_term_fact_crystal(candidate, project):
            continue
        candidates[candidate["id"]] = candidate
    return list(candidates.values())


def source_refs_from_metadata(metadata: _worker.JsonDict) -> set[str]:
    refs = set(list_from_value(metadata.get("source_refs") or []))
    refs.update(list_from_value(metadata.get("source_refs_sample") or []))
    refs.update(list_from_value(metadata.get("crystallized_from") or []))
    return refs


def consolidation_score(candidate: _worker.JsonDict, batch: _worker.JsonDict, crystal_payload: _worker.JsonDict) -> float:
    metadata = candidate.get("metadata") or {}
    payload_metadata = crystal_payload["metadata"]
    if candidate.get("upsert_key") == crystal_payload.get("upsert_key"):
        return 1.0

    candidate_concepts = set(candidate.get("concepts") or [])
    payload_concepts = set(crystal_payload.get("concepts") or [])
    candidate_files = set(candidate.get("files") or [])
    payload_files = set(crystal_payload.get("files") or [])
    candidate_refs = source_refs_from_metadata(metadata)
    payload_refs = source_refs_from_metadata(payload_metadata)
    candidate_tokens = token_set(candidate.get("content"))
    payload_tokens = token_set(crystal_payload.get("content"))

    score = 0.0
    if metadata.get("problem_entity") == payload_metadata.get("problem_entity"):
        score += 0.32
    if metadata.get("crystallized_group") == payload_metadata.get("crystallized_group"):
        score += 0.28
    if _worker.safe_str(metadata.get("domain")).lower() == _worker.safe_str(payload_metadata.get("domain")).lower():
        score += 0.08
    if batch["topic"] in candidate_tokens or batch["topic"] in candidate_concepts:
        score += 0.08

    score += jaccard(candidate_concepts, payload_concepts) * 0.26
    score += jaccard(candidate_files, payload_files) * 0.18
    score += jaccard(candidate_refs, payload_refs) * 0.26
    score += jaccard(candidate_tokens, payload_tokens) * 0.34
    return min(score, 1.0)


def consolidated_content(base: _worker.JsonDict, crystal_payload: _worker.JsonDict, redundant: list[_worker.JsonDict]) -> str:
    base_content = _worker.safe_str(base.get("content")).strip()
    new_content = _worker.safe_str(crystal_payload.get("content")).strip()
    base_tokens = token_set(base_content)
    new_tokens = token_set(new_content)
    lines: list[str] = []

    if base_content.lower().startswith("architectural_law:"):
        lines.append(base_content)
    else:
        lines.extend(["architectural_law:", base_content])

    if new_content and new_content not in base_content and jaccard(base_tokens, new_tokens) < 0.82:
        lines.extend(["", f"consolidated_update: {_worker.utc_now()}", new_content])

    if redundant:
        lines.extend(["", "archived_redundant_crystals:"])
        for item in redundant[:20]:
            lines.append(f"- {item['id']}")

    return _worker.trim_text("\n".join(line for line in lines if line is not None).strip(), _worker.CONSOLIDATED_CONTENT_LIMIT)


def merge_consolidation_metadata(base: _worker.JsonDict, redundant: list[_worker.JsonDict], crystal_payload: _worker.JsonDict) -> _worker.JsonDict:
    base_metadata = base.get("metadata") or {}
    payload_metadata = crystal_payload["metadata"]
    merged_from = list_from_value(base_metadata.get("consolidated_from") or [])
    merged_from.extend(item["id"] for item in redundant)
    merged_from.append(crystal_payload["upsert_key"])
    source_refs = sorted(source_refs_from_metadata(base_metadata) | source_refs_from_metadata(payload_metadata))

    return {
        "lifecycle": "validated",
        "retention": "long-term",
        "semantic_type": payload_metadata.get("semantic_type") or base_metadata.get("semantic_type") or "knowledge",
        "domain": payload_metadata.get("domain") or base_metadata.get("domain") or "infra",
        "priority": payload_metadata.get("priority") or base_metadata.get("priority") or "high",
        "version": "1.1",
        "consolidated_at": _worker.utc_now(),
        "consolidated_by": _worker.WORKER_NAME,
        "consolidation_strategy": "semantic-law-merge",
        "consolidated_from": unique_strings(merged_from, _worker.DEFAULT_REPORT_LIST_LIMIT),
        "consolidated_from_count": len(set(merged_from)),
        "source_refs": source_refs[:_worker.DEFAULT_REPORT_LIST_LIMIT],
        "source_refs_count": len(source_refs),
        "source_refs_truncated": len(source_refs) > _worker.DEFAULT_REPORT_LIST_LIMIT,
    }


async def consolidate_long_term_crystal(
    args: _worker.argparse.Namespace,
    batch: _worker.JsonDict,
    crystal_payload: _worker.JsonDict,
) -> _worker.JsonDict:
    metadata = crystal_payload["metadata"]
    if metadata.get("semantic_type") not in _worker.CRYSTAL_SEMANTIC_TYPES and metadata.get("semantic_type") != "fact":
        return {"applied": False, "reason": "not_fact_crystal"}
    if metadata.get("retention") != "long-term":
        return {"applied": False, "reason": "not_long_term_fact"}

    candidates = await collect_consolidation_candidates(args, batch, crystal_payload)
    scored = [
        {**candidate, "consolidation_score": consolidation_score(candidate, batch, crystal_payload)}
        for candidate in candidates
    ]
    scored = [candidate for candidate in scored if candidate["consolidation_score"] >= _worker.CONSOLIDATION_MIN_SCORE]
    if not scored:
        return {"applied": False, "reason": "no_overlap", "candidate_count": len(candidates)}

    scored.sort(
        key=lambda item: (
            float(item.get("consolidation_score") or 0.0),
            int((item.get("metadata") or {}).get("crystallized_source_count") or 0),
            _worker.safe_str(item.get("updated_at") or item.get("created_at")),
        ),
        reverse=True,
    )
    base = scored[0]
    base_id = base["id"]
    redundant = [
        item
        for item in scored[1:]
        if item["id"] != base_id
        and (
            item["consolidation_score"] >= _worker.CONSOLIDATION_ARCHIVE_SCORE
            or (item.get("metadata") or {}).get("problem_entity") == metadata.get("problem_entity")
        )
    ]

    merged_content = consolidated_content(base, crystal_payload, redundant)
    merged_concepts = unique_strings(
        list(base.get("concepts") or []) + list(crystal_payload.get("concepts") or []),
        80,
    )
    merged_files = unique_strings(list(base.get("files") or []) + list(crystal_payload.get("files") or []), 80)
    metadata_patch = merge_consolidation_metadata(base, redundant, crystal_payload)

    result = await _worker.async_rest_call(
        args.bhm_base_url,
        "POST",
        "/bhm/memory/update",
        {
            "id": base_id,
            "project": batch["project"],
            "type": "knowledge-crystal",
            "content": merged_content,
            "concepts": merged_concepts,
            "files": merged_files,
            "metadata_patch": metadata_patch,
        },
        args.timeout,
    )
    await source_refs_attach(args, base_id, batch["project"], crystal_payload["metadata"].get("source_refs") or [])

    archived_redundant = 0
    if redundant:
        archive_result = await _worker.async_rest_call(
            args.bhm_base_url,
            "POST",
            "/bhm/memories/batch-archive",
            {
                "items": [
                    {
                        "id": item["id"],
                        "project": batch["project"],
                        "reason": f"consolidated_into:{base_id}",
                    }
                    for item in redundant
                ]
            },
            args.timeout,
        )
        archived_redundant = int(archive_result.get("count") or len(redundant))

    return {
        "applied": True,
        "crystal_id": base_id,
        "base_id": base_id,
        "matched_count": len(scored),
        "archived_redundant_count": archived_redundant,
        "source_refs_attached": len(crystal_payload["metadata"].get("source_refs") or []),
        "update_action": result.get("success", True),
    }


def build_upsert_key(batch: _worker.JsonDict) -> str:
    archive_ids = sorted(_worker.safe_str(value) for value in batch.get("archive_targets") or [])
    if not archive_ids:
        archive_ids = sorted(_worker.safe_str(record.get("id")) for record in batch["records"])
    digest = _worker.stable_checksum(
        f"{batch['project']}|{batch['topic']}|{batch.get('tier')}|"
        f"{batch.get('total_pool_count')}|{'|'.join(archive_ids)}"
    )
    topic = _worker.re.sub(r"[^a-zA-Z0-9_.:-]+", "-", _worker.safe_str(batch["topic"])).strip("-") or "general"
    return f"knowledge-crystal:{batch['project']}:{topic}:{digest}"


def _matches_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(_worker.re.search(pattern, text, _worker.re.IGNORECASE) for pattern in patterns)


def classify_crystal_vector_targets(batch: _worker.JsonDict, crystal_payload: _worker.JsonDict) -> tuple[list[str], str]:
    metadata = crystal_payload.get("metadata") or {}
    text = "\n".join(
        [
            _worker.safe_str(crystal_payload.get("content")),
            " ".join(_worker.safe_str(concept) for concept in crystal_payload.get("concepts") or []),
            " ".join(_worker.safe_str(file_path) for file_path in crystal_payload.get("files") or []),
            _worker.safe_str(metadata.get("problem_entity")),
            _worker.safe_str(metadata.get("domain")),
            _worker.safe_str(metadata.get("synthesis_mode")),
            batch.get("topic") or "",
        ]
    )
    if _matches_any_pattern(text, _worker.LOCAL_ENTITY_VECTOR_PATTERNS):
        return ["local"], "local-entities"
    if _matches_any_pattern(text, _worker.SYSTEM_INSIGHT_VECTOR_PATTERNS):
        return ["local", "global"], "system-insight"
    return ["local"], "project-context"


def normalize_synthesis_importance(value: _worker.Any, default: int = 5) -> int:
    try:
        score = int(value)
    except (TypeError, ValueError):
        score = default
    return max(1, min(score, 10))


def normalize_synthesis_linked_dependencies(value: _worker.Any) -> list[_worker.JsonDict]:
    if not isinstance(value, list):
        return []
    dependencies: list[_worker.JsonDict] = []
    seen: set[tuple[str, str]] = set()
    for item in value:
        if not isinstance(item, dict):
            continue
        keyword = _worker.trim_text(
            _worker.safe_str(
                item.get("target_core_insight_keyword")
                or item.get("target_keyword")
                or item.get("keyword")
            ),
            240,
        )
        edge_type = _worker.safe_str(item.get("edge_type")).strip().upper()
        if not keyword or edge_type not in _worker.VALID_SEMANTIC_EDGE_TYPES:
            continue
        key = (keyword.casefold(), edge_type)
        if key in seen:
            continue
        seen.add(key)
        dependencies.append({"target_core_insight_keyword": keyword, "edge_type": edge_type})
    return dependencies[:10]


def synthesis_taxonomy_text(batch: _worker.JsonDict, synthesis_fact: _worker.JsonDict, crystal_text: str) -> str:
    records = batch.get("archive_records") or batch.get("records") or []
    record_text = " ".join(
        " ".join(
            _worker.safe_str(record.get(key))
            for key in ("content", "domain", "semantic_type", "priority", "memory_type")
        )
        for record in records[:80]
    )
    return " ".join(
        [
            _worker.safe_str(batch.get("project")),
            _worker.safe_str(batch.get("topic")),
            crystal_text,
            _worker.json.dumps(synthesis_fact, ensure_ascii=False, sort_keys=True),
            record_text,
        ]
    ).lower()


def normalize_crystal_domain(value: _worker.Any, batch: _worker.JsonDict, synthesis_fact: _worker.JsonDict, crystal_text: str) -> str:
    candidate = _worker.safe_str(value).strip().lower()
    if candidate in _worker.VALID_DOMAINS:
        return candidate
    text = synthesis_taxonomy_text(batch, synthesis_fact, crystal_text)
    if any(token in text for token in ("secret", "token", "auth", "permission", "vulnerab", "security", "hard-delete")):
        return "security"
    if any(token in text for token in ("ui", "interface", "color", "layout", "canvas", "three.js", "html", "css", "react", "screenshot")):
        return "frontend"
    if any(token in text for token in ("fastapi", "endpoint", "route", "api", "adapter", "service logic", "backend")):
        return "backend"
    if any(token in text for token in ("roadmap", "requirement", "acceptance criteria", "user workflow", "product")):
        return "product"
    if any(token in text for token in ("docker", "wsl", "powershell", "qdrant", "mem0", "mcp", "runtime", "worker", "port", "deploy")):
        return "infra"
    return majority(batch.get("archive_records") or batch.get("records") or [], "domain", "general", _worker.VALID_DOMAINS)


def normalize_crystal_priority(value: _worker.Any, batch: _worker.JsonDict, synthesis_fact: _worker.JsonDict, crystal_text: str) -> str:
    candidate = _worker.safe_str(value).strip().lower()
    candidate = {"normal": "medium", "trivial": "low"}.get(candidate, candidate)
    if candidate in _worker.CRYSTAL_PRIORITIES:
        return candidate
    text = synthesis_taxonomy_text(batch, synthesis_fact, crystal_text)
    if any(token in text for token in ("data loss", "corrupt", "secret", "token", "fatal", "critical outage")):
        return "critical"
    if any(token in text for token in ("error", "crash", "failed", "failure", "timeout", "traceback", "exception", "regression", "broken validation")):
        return "high"
    return "medium"


def normalize_crystal_semantic_type(value: _worker.Any, batch: _worker.JsonDict, synthesis_fact: _worker.JsonDict, crystal_text: str) -> str:
    candidate = _worker.safe_str(value).strip().lower()
    candidate = {
        "fact": "knowledge",
        "log": "knowledge",
        "error": "bugfix",
        "decision-log": "architecture",
        "requirement": "feature",
    }.get(candidate, candidate)
    if candidate in _worker.CRYSTAL_SEMANTIC_TYPES:
        return candidate
    text = synthesis_taxonomy_text(batch, synthesis_fact, crystal_text)
    if any(token in text for token in ("architecture", "contract", "adr", "decision", "cross-component")):
        return "architecture"
    if any(token in text for token in ("bug", "root cause", "regression", "error", "traceback", "exception", "failed", "failure")):
        return "bugfix"
    if any(token in text for token in ("feature", "capability", "implement", "delivery", "requirement")):
        return "feature"
    if any(token in text for token in ("refactor", "restructure", "cleanup", "migration")):
        return "refactor"
    return "knowledge"


def build_crystal_payload(
    batch: _worker.JsonDict,
    crystal_text: str,
    synthesis_mode: str,
    synthesis_fact: _worker.JsonDict | None = None,
) -> _worker.JsonDict:
    synthesis_fact = synthesis_fact if isinstance(synthesis_fact, dict) else {}
    records = batch["records"]
    archive_records = batch.get("archive_records") or records
    tier = _worker.safe_str(batch.get("tier") or _worker.TIER_MICRO)
    archive_source_ids = [_worker.safe_str(record.get("id")) for record in archive_records]
    source_ids = archive_source_ids if tier != _worker.TIER_BIG_BANG else archive_source_ids[:_worker.DEFAULT_REPORT_LIST_LIMIT]
    prompt_source_ids = [_worker.safe_str(record.get("id")) for record in records]
    source_memory_ids_all = [_worker.safe_str(record.get("id")) for record in archive_records if record.get("source_kind") == "memory"]
    source_observation_ids_all = [
        _worker.safe_str(record.get("id")) for record in archive_records if record.get("source_kind") == "observation"
    ]
    source_memory_ids = (
        source_memory_ids_all if tier != _worker.TIER_BIG_BANG else source_memory_ids_all[:_worker.DEFAULT_REPORT_LIST_LIMIT]
    )
    source_observation_ids = (
        source_observation_ids_all
        if tier != _worker.TIER_BIG_BANG
        else source_observation_ids_all[:_worker.DEFAULT_REPORT_LIST_LIMIT]
    )
    concepts = sorted(
        {
            "knowledge-crystal",
            "fact-crystal",
            "crystallizer",
            batch["project"],
            batch["topic"],
            *[
                _worker.safe_str(concept)
                for record in records
                for concept in record.get("concepts") or []
                if _worker.safe_str(concept)
            ],
        }
    )[:30]
    files = sorted({_worker.safe_str(file_path) for record in records for file_path in record.get("files") or [] if _worker.safe_str(file_path)})[:30]
    fallback_priority = (
        "critical"
        if tier in {_worker.TIER_SINGULARITY, _worker.TIER_BIG_BANG}
        or any(record.get("priority") == "critical" for record in archive_records)
        else "high"
    )
    domain = normalize_crystal_domain(synthesis_fact.get("domain"), batch, synthesis_fact, crystal_text)
    priority = normalize_crystal_priority(synthesis_fact.get("priority") or fallback_priority, batch, synthesis_fact, crystal_text)
    semantic_type = normalize_crystal_semantic_type(synthesis_fact.get("semantic_type"), batch, synthesis_fact, crystal_text)
    source_refs_all = [source_ref(record) for record in archive_records]
    source_refs = [] if tier == _worker.TIER_BIG_BANG else source_refs_all
    source_refs_sample = source_refs_all[:_worker.DEFAULT_REPORT_LIST_LIMIT]
    payload = {
        "upsert_key": build_upsert_key(batch),
        "project": batch["project"],
        "type": "knowledge-crystal",
        "content": crystal_text,
        "concepts": concepts,
        "files": files,
        "metadata": {
            "lifecycle": "validated",
            "provenance": "llm" if synthesis_mode == "bhm-rest" else "synthetic",
            "priority": priority,
            "domain": domain,
            "sensitivity": "internal",
            "scope": "service",
            "retention": "long-term",
            "verification": "unverified",
            "actionability": "info",
            "stakeholder": "core-team",
            "language": majority(archive_records, "language", "en", _worker.VALID_LANGUAGES),
            "semantic_type": semantic_type,
            "version": "1.0",
            "importance_score": normalize_synthesis_importance(synthesis_fact.get("importance_score"), 5),
            "access_count": 1,
            "last_accessed_at": _worker.utc_now(),
            "linked_dependencies": normalize_synthesis_linked_dependencies(synthesis_fact.get("linked_dependencies")),
            "problem_entity": f"{batch['project']}::{batch['topic']}",
            "root_cause_required": True,
            "architecture_impact_required": True,
            "project_hub_ref": f"project::{batch['project']}",
            "crystallized_by": _worker.WORKER_NAME,
            "crystallized_at": _worker.utc_now(),
            "crystallized_group": batch["group_key"],
            "crystallized_group_kind": batch["group_kind"],
            "scale_tier": tier,
            "total_pool_count": int(batch.get("total_pool_count") or len(archive_records)),
            "prompt_sample_count": int(batch.get("prompt_sample_count") or len(records)),
            "archive_target_count": len(archive_source_ids),
            "archive_targets_digest": _worker.stable_checksum("|".join(sorted(archive_source_ids))),
            "crystallized_source_count": int(batch.get("total_pool_count") or len(archive_source_ids)),
            "crystallized_from": source_ids,
            "crystallized_from_count": len(archive_source_ids),
            "crystallized_from_truncated": tier == _worker.TIER_BIG_BANG and len(archive_source_ids) > len(source_ids),
            "source_sample_ids": prompt_source_ids,
            "source_memory_ids": source_memory_ids,
            "source_memory_ids_count": len(source_memory_ids_all),
            "source_memory_ids_truncated": tier == _worker.TIER_BIG_BANG
            and len(source_memory_ids_all) > len(source_memory_ids),
            "source_observation_ids": source_observation_ids,
            "source_observation_ids_count": len(source_observation_ids_all),
            "source_observation_ids_truncated": tier == _worker.TIER_BIG_BANG
            and len(source_observation_ids_all) > len(source_observation_ids),
            "source_refs": source_refs,
            "source_refs_sample": source_refs_sample,
            "source_refs_omitted": tier == _worker.TIER_BIG_BANG,
            "context_temperature_model": "active/compress/frozen",
            "context_zones": batch.get("context_zones") or {},
            "synthesis_mode": synthesis_mode,
        },
    }
    vector_targets, vector_reason = classify_crystal_vector_targets(batch, payload)
    metadata = payload["metadata"]
    metadata["vector_targets"] = vector_targets
    metadata["vector_scope"] = "local+global" if "global" in vector_targets else "local"
    metadata["vector_target_reason"] = vector_reason
    if "global" in vector_targets:
        metadata["global_collection_name"] = _worker.GLOBAL_CORE_COLLECTION_NAME
    return payload


async def upsert_crystal(args: _worker.argparse.Namespace, payload: _worker.JsonDict) -> str:
    result = await _worker.async_rest_call(args.bhm_base_url, "POST", "/bhm/memory/upsert", payload, args.timeout)
    memory = result.get("memory")
    if not isinstance(memory, dict) or not memory.get("id"):
        raise _worker.SoftFail(f"BHM upsert did not return memory id: {_worker.json.dumps(result, ensure_ascii=False)[:1200]}")
    return _worker.safe_str(memory["id"])


async def source_refs_attach(args: _worker.argparse.Namespace, crystal_id: str, project: str, refs: list[str]) -> None:
    if not refs:
        return
    await _worker.async_rest_call(
        args.bhm_base_url,
        "POST",
        "/bhm/memory/source-refs",
        {"id": crystal_id, "project": project, "refs": refs},
        args.timeout,
    )


async def get_or_rebuild_project_hub(args: _worker.argparse.Namespace, project: str) -> str | None:
    try:
        result = await _worker.async_rest_call(
            args.bhm_base_url,
            "GET",
            "/bhm/project-summary",
            None,
            args.timeout,
            {"project": project},
        )
        memory = result.get("memory")
        if isinstance(memory, dict) and memory.get("id"):
            return _worker.safe_str(memory["id"])
    except _worker.SoftFail:
        pass
    result = await _worker.async_rest_call(
        args.bhm_base_url,
        "POST",
        "/bhm/project-summary/rebuild",
        {"project": project},
        args.timeout,
    )
    memory = result.get("memory")
    if isinstance(memory, dict) and memory.get("id"):
        return _worker.safe_str(memory["id"])
    return None


def link_items_for_batch(crystal_id: str, batch: _worker.JsonDict, project_hub_id: str | None) -> list[_worker.JsonDict]:
    records = batch["records"]
    metadata = {
        "lifecycle": "validated",
        "provenance": "synthetic",
        "priority": "medium",
        "domain": majority(records, "domain", "general", _worker.VALID_DOMAINS),
        "sensitivity": "internal",
        "scope": "service",
        "retention": "long-term",
        "verification": "trusted",
        "actionability": "info",
        "stakeholder": "core-team",
        "language": majority(records, "language", "en", _worker.VALID_LANGUAGES),
        "semantic_type": "knowledge",
        "version": "1.0",
        "scale_tier": _worker.safe_str(batch.get("tier") or _worker.TIER_MICRO),
        "crystallized_source_count": int(batch.get("total_pool_count") or len(records)),
    }
    links: list[_worker.JsonDict] = []
    for record in records:
        if record.get("source_kind") != "memory":
            continue
        if record.get("project") != batch["project"]:
            continue
        links.append(
            {
                "source_id": crystal_id,
                "target_id": record["id"],
                "relation": "condenses",
                "project": batch["project"],
                "metadata": {**metadata, "source_ref": source_ref(record)},
            }
        )
    if project_hub_id:
        links.append(
            {
                "source_id": crystal_id,
                "target_id": project_hub_id,
                "relation": "belongs_to_project_hub",
                "project": batch["project"],
                "metadata": {**metadata, "project_hub_ref": f"project::{batch['project']}"},
            }
        )
    return links


async def create_links(args: _worker.argparse.Namespace, links: list[_worker.JsonDict]) -> _worker.JsonDict:
    if not links:
        return {"created_count": 0, "method": "none", "chunk_size": _worker.BATCH_REST_CHUNK_SIZE, "chunks": 0}
    link_chunks = list(chunked(links, _worker.BATCH_REST_CHUNK_SIZE))
    results = []
    for chunk in link_chunks:
        result = await _worker.async_rest_call(args.bhm_base_url, "POST", "/bhm/memories/batch-link", {"items": chunk}, args.timeout)
        results.append(result)
    created = 0
    for result in results:
        created += int(result.get("count") or len(result.get("links") or []))
    return {
        "created_count": created,
        "method": "batch_link_chunks",
        "chunk_size": _worker.BATCH_REST_CHUNK_SIZE,
        "chunks": len(link_chunks),
    }


async def try_bulk_archive_by_filter(
    args: _worker.argparse.Namespace,
    batch: _worker.JsonDict,
    crystal_id: str,
    expected_count: int,
) -> _worker.JsonDict | None:
    if batch.get("tier") != _worker.TIER_BIG_BANG or expected_count <= _worker.BATCH_REST_CHUNK_SIZE:
        return None
    payload = {
        "project": batch["project"],
        "topic": batch["topic"],
        "action": "bulk_archive",
    }
    try:
        result = await _worker.async_rest_call(args.bhm_base_url, "POST", _worker.BULK_ARCHIVE_ENDPOINT, payload, args.timeout)
    except _worker.SoftFail as exc:
        if "HTTP 404" in str(exc) or "HTTP 405" in str(exc):
            return None
        raise
    return {
        "archived_count": int(result.get("count") or result.get("archived_count") or expected_count),
        "method": "bulk_archive_filter",
        "endpoint": _worker.BULK_ARCHIVE_ENDPOINT,
        "chunk_size": 0,
        "chunks": 1,
        "expected_count": expected_count,
        "crystal_id": crystal_id,
    }


async def archive_live_memory_sources(args: _worker.argparse.Namespace, batch: _worker.JsonDict, crystal_id: str) -> _worker.JsonDict:
    archive_records = batch.get("archive_records") or batch["records"]
    items = [
        {
            "id": _worker.safe_str(record.get("id")),
            "project": _worker.safe_str(record.get("project")),
            "reason": f"condensed into knowledge crystal {crystal_id}",
        }
        for record in archive_records
        if record.get("source_kind") == "memory"
    ]
    if not items:
        return {
            "archived_count": 0,
            "method": "none",
            "chunk_size": _worker.BATCH_REST_CHUNK_SIZE,
            "chunks": 0,
            "expected_count": 0,
        }
    high_tier = batch.get("tier") in {_worker.TIER_SINGULARITY, _worker.TIER_BIG_BANG}
    item_chunks = list(chunked(items, _worker.BATCH_REST_CHUNK_SIZE))

    async def archive_chunk(chunk: list[_worker.JsonDict]) -> _worker.JsonDict:
        return await _worker.async_rest_call(
            args.bhm_base_url,
            "POST",
            "/bhm/memories/batch-archive",
            {"items": chunk},
            args.timeout,
        )

    if high_tier:
        results = await _worker.asyncio.gather(*(archive_chunk(chunk) for chunk in item_chunks))
        method = "parallel_batch_archive_chunks"
    else:
        results = []
        for chunk in item_chunks:
            results.append(await archive_chunk(chunk))
        method = "batch_archive_chunks"

    archived = 0
    for chunk, result in zip(item_chunks, results, strict=True):
        archived += int(result.get("count") or result.get("archived_count") or len(chunk))
    return {
        "archived_count": archived,
        "method": method,
        "chunk_size": _worker.BATCH_REST_CHUNK_SIZE,
        "chunks": len(item_chunks),
        "expected_count": len(items),
        "parallel": high_tier,
    }



