"""Deterministic offline gate for the P17.19 privacy-bounded LLM cache."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from blackholememory.llm_cache import LLM_CACHE_POLICY_VERSION
from blackholememory.llm_cache import LLMCachePrivacyError
from blackholememory.llm_cache import LLMCacheStore
from blackholememory.llm_cache import build_cache_identity
from blackholememory.llm_cache import build_cache_preview


def _identity(content: str, *, project: str = "demo"):
    return build_cache_identity(
        content,
        "shared system prefix\nanswer the bounded task",
        project=project,
        prompt_version="prompt-v1",
        model_digest="model-a",
        parameters={"temperature": 0, "seed": 7},
        prompt_prefix="shared system prefix",
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bhm-p17.19-") as directory:
        store = LLMCacheStore(Path(directory) / "cache.sqlite3", max_entries=4, ttl_seconds=60)
        first = _identity("first")
        second = _identity("second")
        stored = store.put(first, {"answer": "bounded result"})
        exact = store.get(first, include_result=True)
        prefix = store.find_prefix(second)
        invalidated = store.invalidate(project="demo", model_digest="model-a", reason="model refresh")
        after_invalidation = store.get(first)
        blocked = build_cache_identity(
            {"api_key": "secret"},
            "safe prompt",
            project="demo",
            prompt_version="prompt-v1",
            model_digest="model-a",
        )
        blocked_preview = build_cache_preview(
            blocked
        )
        privacy_write_rejected = False
        try:
            store.put(blocked, {"answer": "safe"})
        except LLMCachePrivacyError:
            privacy_write_rejected = True

    checks = {
        "schema": blocked_preview["schema_version"] == LLM_CACHE_POLICY_VERSION,
        "exact_reuse": stored["cache_key"] == first.cache_key and exact and exact["result"] == {"answer": "bounded result"},
        "prefix_reuse": len(prefix) == 1 and prefix[0]["cache_key"] == first.cache_key,
        "invalidation": invalidated["invalidated"] == 1 and after_invalidation is None,
        "privacy_block": blocked_preview["privacy"]["cacheable"] is False,
        "privacy_write_rejected": privacy_write_rejected,
        "no_raw_output": "api_key" not in json.dumps(blocked_preview, ensure_ascii=False),
        "proposal_only": blocked_preview["execution_enabled"] is False and blocked_preview["writes_performed"] is False,
    }
    report = {
        "ok": all(checks.values()),
        "schema_version": LLM_CACHE_POLICY_VERSION,
        "checks": checks,
        "execution_enabled": False,
        "writes_performed": False,
        "auto_apply": False,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
