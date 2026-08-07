#!/usr/bin/env python3
# ruff: noqa: E402
"""Dry-run-first autonomous Night Watch orchestrator for BHM technical debt."""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import asdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.runtime_endpoints import endpoint_url
from blackholememory.local_endpoint_policy import LocalEndpointError
from blackholememory.local_endpoint_policy import open_local_url
from blackholememory.local_endpoint_policy import read_bounded_response
from blackholememory.local_endpoint_policy import validate_local_endpoint
from blackholememory.resource_limits import BHM_INTERNAL_HTTP_TIMEOUT_SECONDS

DEFAULT_BHM_BASE_URL = endpoint_url("bhm_api")
DEFAULT_PROJECT = "BlackHoleMemory"
# Compatibility name retained; the registry-backed internal BHM bound is canonical.
DEFAULT_TIMEOUT_SECONDS = float(BHM_INTERNAL_HTTP_TIMEOUT_SECONDS)
DEFAULT_QUERY_LIMIT = 5
DEFAULT_TARGET_LIMIT = 2
MAX_HTTP_RESPONSE_BYTES = 256 * 1024
MAX_CONTEXT_CHARS = 1400
RUNTIME_RELEASE = "v0.8.0-PURE-NIGHT-WATCH"

DEBT_QUERIES = (
    "технический долг",
    "TODO",
    "требует рефакторинга",
    "уязвимость",
    "technical debt",
    "refactor code",
    "TODO code",
    "vulnerability code",
)

PROBLEM_MARKERS = (
    "bug",
    "technical debt",
    "технический долг",
    "todo",
    "fixme",
    "failure",
    "http 500",
    "race",
    "refactor",
    "рефактор",
    "требует рефакторинга",
    "vulnerab",
    "уязвим",
    "security",
    "risk",
    "debt",
)

NEGATIVE_TARGET_MARKERS = (
    "smoke",
    "healthz",
    "demo",
    "diagnostic",
    "quarantine",
    "super-crystal",
    "qdrant is healthy",
    "release-finalization",
)

CODE_MARKERS = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    "scripts/",
    "scripts\\",
    "src/",
    "src\\",
    "tests/",
    "tests\\",
)


JsonDict = dict[str, Any]


def bounded_night_watch_timeout(value: float) -> float:
    """Clamp Night Watch internal BHM REST waits to the shared finite bound."""

    try:
        requested = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Night Watch timeout must be numeric") from exc
    if not math.isfinite(requested):
        raise ValueError("Night Watch timeout must be finite")
    return max(min(requested, float(BHM_INTERNAL_HTTP_TIMEOUT_SECONDS)), 1.0)


@dataclass(frozen=True)
class NightWatchTarget:
    id: str
    query: str
    project: str
    score: float
    content: str
    title: str
    memory_type: str
    files: list[str]
    metadata: JsonDict

    def excerpt(self, limit: int = 360) -> str:
        text = normalize_space(self.content)
        if len(text) <= limit:
            return text
        return f"{text[: limit - 3]}..."


def normalize_space(value: Any) -> str:
    return " ".join(str(value or "").split())


def text_or_empty(value: Any) -> str:
    return str(value or "").strip()


def list_of_strings(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        return [text] if text else []
    if isinstance(value, list):
        return [text for item in value if (text := text_or_empty(item))]
    return []


def nested_metadata(item: JsonDict) -> JsonDict:
    metadata = item.get("metadata")
    return dict(metadata) if isinstance(metadata, dict) else {}


def memory_content(item: JsonDict) -> str:
    for key in ("content", "memory", "summary", "text", "result"):
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, dict):
            nested = memory_content(value)
            if nested:
                return nested
    return ""


def memory_id(item: JsonDict) -> str:
    metadata = nested_metadata(item)
    return text_or_empty(
        item.get("id")
        or item.get("source_id")
        or item.get("obsId")
        or metadata.get("source_id")
        or metadata.get("id")
    )


def memory_type(item: JsonDict) -> str:
    metadata = nested_metadata(item)
    return text_or_empty(
        item.get("type")
        or item.get("memory_type")
        or metadata.get("type")
        or metadata.get("memory_type")
    )


def memory_project(item: JsonDict, default_project: str) -> str:
    metadata = nested_metadata(item)
    return text_or_empty(item.get("project") or metadata.get("project") or default_project)


def response_items(data: JsonDict) -> list[JsonDict]:
    for key in ("memories", "results", "matches", "items"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


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


def post_json(base_url: str, path: str, payload: JsonDict, timeout: int) -> JsonDict:
    bounded_timeout = bounded_night_watch_timeout(timeout)
    try:
        validated_base_url = validate_local_endpoint(base_url)
    except LocalEndpointError as exc:
        raise RuntimeError(f"BHM endpoint rejected by local transport policy: {exc}") from exc
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {_required_bhm_caller_token()}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        with open_local_url(request, timeout=bounded_timeout, endpoint=validated_base_url) as response:
            raw = read_bounded_response(response, limit=MAX_HTTP_RESPONSE_BYTES).decode("utf-8")
    except urllib.error.HTTPError as exc:
        try:
            detail = read_bounded_response(exc, limit=4096).decode("utf-8", errors="replace")[:800]
        except LocalEndpointError:
            detail = "[response body exceeded bounded error limit]"
        raise RuntimeError(f"BHM POST {path} failed with HTTP {exc.code}: {detail}") from exc
    except LocalEndpointError as exc:
        raise RuntimeError(f"BHM POST {path} rejected by local transport policy: {exc}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"BHM POST {path} unavailable: {exc}") from exc

    if not raw.strip():
        return {}
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise RuntimeError(f"BHM POST {path} returned non-object JSON")
    return data


def score_target(item: JsonDict, query: str, default_project: str) -> NightWatchTarget | None:
    metadata = nested_metadata(item)
    content = memory_content(item)
    target_id = memory_id(item)
    if not target_id or not content:
        return None

    lifecycle = text_or_empty(metadata.get("lifecycle") or item.get("lifecycle")).lower()
    if lifecycle in {"archived", "deprecated"}:
        return None

    semantic_type = text_or_empty(metadata.get("semantic_type") or item.get("semantic_type")).lower()
    item_type = memory_type(item)
    files = list_of_strings(item.get("files") or metadata.get("files"))
    title = text_or_empty(
        item.get("title")
        or metadata.get("raw_title")
        or metadata.get("title")
        or target_id
    )
    haystack = " ".join(
        [
            content,
            title,
            item_type,
            semantic_type,
            " ".join(files),
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
        ]
    ).lower()
    if any(marker in haystack for marker in NEGATIVE_TARGET_MARKERS):
        return None

    raw_score = float(item.get("score") or metadata.get("score") or 0.0)
    score = raw_score
    if item_type == "knowledge-crystal":
        score += 1.0
    if semantic_type == "fact" or lifecycle == "validated":
        score += 0.6
    if any(marker in haystack for marker in PROBLEM_MARKERS):
        score += 1.3
    if any(marker in haystack for marker in CODE_MARKERS):
        score += 0.7
    if files:
        score += 0.5
    if query.lower() in haystack:
        score += 0.4

    is_crystal_like = item_type == "knowledge-crystal" or semantic_type == "fact" or lifecycle == "validated"
    is_problem_like = score >= raw_score + 1.0 or any(marker in haystack for marker in PROBLEM_MARKERS)
    if not (is_crystal_like and is_problem_like):
        return None

    return NightWatchTarget(
        id=target_id,
        query=query,
        project=memory_project(item, default_project),
        score=round(score, 4),
        content=content,
        title=title,
        memory_type=item_type or semantic_type or "memory",
        files=files,
        metadata=metadata,
    )


def find_targets(args: argparse.Namespace) -> list[NightWatchTarget]:
    targets_by_id: dict[str, NightWatchTarget] = {}
    for query in DEBT_QUERIES:
        payload = {
            "query": query,
            "project": args.project,
            "limit": args.query_limit,
            "include_archived": False,
            "include_logs": False,
        }
        print(f"[night-watch] search query: {query!r}")
        data = post_json(args.bhm_base_url, "/bhm/search", payload, args.timeout)
        for item in response_items(data):
            target = score_target(item, query, args.project)
            if target is None:
                continue
            previous = targets_by_id.get(target.id)
            if previous is None or target.score > previous.score:
                targets_by_id[target.id] = target

    return sorted(targets_by_id.values(), key=lambda item: item.score, reverse=True)[: args.targets]


def build_agent_task(targets: list[NightWatchTarget]) -> str:
    context_blocks = []
    for index, target in enumerate(targets, start=1):
        files = ", ".join(target.files[:8]) if target.files else "not specified"
        context_blocks.append(
            "\n".join(
                [
                    f"Target {index}",
                    f"id: {target.id}",
                    f"query: {target.query}",
                    f"type: {target.memory_type}",
                    f"files: {files}",
                    f"context: {target.excerpt(MAX_CONTEXT_CHARS)}",
                ]
            )
        )
    context = "\n\n".join(context_blocks)
    return (
        "Ночной Дозор: Обнаружен техдолг. "
        "Изучи этот контекст и проведи рефакторинг.\n\n"
        "Ограничения: работай только с указанным контекстом и связанными файлами; "
        "сначала проверь факты в репозитории; не выполняй разрушительные действия.\n\n"
        f"{context}"
    )


def heal_infrastructure() -> str:
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))
    from blackholememory.tools.infra_healer import tool_check_and_heal_docker

    status = tool_check_and_heal_docker()
    print(f"[night-watch] infra-heal: {status}")
    return status


def run_swarm(args: argparse.Namespace, targets: list[NightWatchTarget]) -> JsonDict:
    if str(SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(SRC_ROOT))

    from blackholememory.agents.developer_agent import BHMAgentExecutor  # noqa: WPS433

    task_id = f"night-watch-{int(time.time())}"
    task = build_agent_task(targets)
    domain = text_or_empty(targets[0].metadata.get("domain")) or "backend"
    print(f"[night-watch] apply: launching BHMAgentExecutor task_id={task_id} domain={domain}")
    executor = BHMAgentExecutor(bhm_base_url=args.bhm_base_url)
    result = executor.execute_loop(task_id, task, domain, args.project)
    return dict(result)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", dest="apply", action="store_false", help="Inspect targets only. This is the default.")
    mode.add_argument("--apply", dest="apply", action="store_true", help="Launch the Developer Agent graph for the selected target.")
    parser.set_defaults(apply=False)
    parser.add_argument("--bhm-base-url", default=DEFAULT_BHM_BASE_URL, help="BHM API base URL.")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="BHM project scope.")
    parser.add_argument("--query-limit", type=int, default=DEFAULT_QUERY_LIMIT, help="Max BHM hits per query.")
    parser.add_argument("--targets", type=int, default=DEFAULT_TARGET_LIMIT, help="Max selected Night Watch targets.")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT_SECONDS, help="BHM HTTP timeout in seconds.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.query_limit = max(1, args.query_limit)
    args.targets = max(1, min(args.targets, 2))
    args.timeout = bounded_night_watch_timeout(args.timeout)

    mode = "apply" if args.apply else "dry-run"
    print(f"[night-watch] runtime={RUNTIME_RELEASE} mode={mode} project={args.project}")
    heal_infrastructure()
    targets = find_targets(args)
    if not targets:
        print("[night-watch] no eligible technical-debt crystal found")
        return 0

    print(f"[night-watch] selected targets: {len(targets)}")
    for index, target in enumerate(targets, start=1):
        print(f"[night-watch] target {index}: id={target.id} score={target.score} query={target.query!r}")
        print(f"[night-watch] target {index} title: {target.title}")
        if target.files:
            print(f"[night-watch] target {index} files: {', '.join(target.files[:8])}")
        print(f"[night-watch] target {index} excerpt: {target.excerpt()}")

    if not args.apply:
        print("[night-watch] dry run only. Re-run with --apply to launch the Developer Agent graph.")
        return 0

    result = run_swarm(args, targets)
    print("[night-watch] swarm result:")
    print(json.dumps({"targets": [asdict(target) for target in targets], "swarm": result}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
