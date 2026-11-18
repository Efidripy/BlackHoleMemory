"""Shared switches for sending text to the paid LLM (opt-in via BHM_LLM_TIER=paid)."""

from __future__ import annotations

import os
from typing import Any

from .config import settings
from .egress_filter import sanitize_for_egress


def paid_ready() -> bool:
    """Explicit operator opt-in plus a complete endpoint; never inferred from a URL."""

    return settings.llm_tier == "paid" and bool(
        settings.paid_llm_base_url and settings.paid_llm_model and settings.paid_llm_api_key
    )


def rerank_uses_paid() -> bool:
    """Rerank runs on every search, so it needs its own opt-in on top of the paid tier."""

    return paid_ready() and os.getenv("BHM_PAID_RERANK", "").strip() == "1"


def auth_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.paid_llm_api_key}"} if rerank_uses_paid() else {}


def local_chat_kwargs() -> dict[str, Any]:
    """Qwen-only request field; the paid endpoint must not receive it."""

    return {} if rerank_uses_paid() else {"chat_template_kwargs": {"enable_thinking": False}}


def outbound(text: str) -> str:
    """Secrets and personal data are stripped from anything sent to the paid model."""

    return sanitize_for_egress(text).text if rerank_uses_paid() else text
