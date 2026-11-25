"""Paid-LLM tier choice of the launcher panel: validation and the environment the BHM API needs."""

from __future__ import annotations

import os
from typing import Any

PAID_TIER_DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
PAID_TIER_DEFAULT_MODEL = "gpt-6-luna"


def validate_paid_tier(llm: dict[str, Any]) -> None:
    if llm.get("tier", "local") not in {"local", "paid"}:
        raise ValueError("launcher settings llm.tier must be local or paid")
    for key in ("paid_base_url", "paid_model"):
        if not isinstance(llm.get(key, ""), str):
            raise ValueError(f"launcher settings llm.{key} must be a string")


def with_llm_tier(settings: dict[str, Any], enabled: bool) -> dict[str, Any]:
    """Copy of the launcher settings with llm.tier set; other llm fields are kept."""

    return {**settings, "llm": {**(settings.get("llm") or {}), "tier": "paid" if enabled else "local"}}


def paid_tier_env(settings: Any) -> dict[str, str]:
    """Environment the BHM API needs for the chosen LLM tier; empty when the panel never set one.

    The API key is deliberately not stored in launcher settings: it comes from the user
    environment variable BHM_PAID_LLM_API_KEY, which the API start script adopts.
    """

    llm = settings.get("llm") if isinstance(settings, dict) else None
    if not isinstance(llm, dict) or "tier" not in llm:
        return {}
    if llm["tier"] != "paid":
        return {"BHM_LLM_TIER": "local"}
    return {
        "BHM_LLM_TIER": "paid",
        "BHM_PAID_LLM_BASE_URL": str(llm.get("paid_base_url") or PAID_TIER_DEFAULT_BASE_URL),
        "BHM_PAID_LLM_MODEL": str(llm.get("paid_model") or PAID_TIER_DEFAULT_MODEL),
    }


def llm_badge(llm: dict[str, Any] | None) -> tuple[str, str]:
    """Sidebar badge (text, kind) telling which LLM the BHM API uses: paid remote, remote URL or local."""

    llm = llm or {}
    # The panel never chose a tier: the API start script then adopts the user environment, so show that.
    tier = llm.get("tier") or os.environ.get("BHM_LLM_TIER", "local")
    if tier == "paid":
        return f"LLM: PAID  {llm.get('paid_model') or PAID_TIER_DEFAULT_MODEL}", "paid"
    if llm.get("mode") == "remote":
        return "LLM: REMOTE", "remote"
    return f"LLM: LOCAL  :{llm.get('port', '')}".rstrip(" :"), "local"
