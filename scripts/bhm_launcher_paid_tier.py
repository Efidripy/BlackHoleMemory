"""Paid-LLM tier choice of the launcher panel: validation and the environment the BHM API needs."""

from __future__ import annotations

import os
import subprocess
from typing import Any

PAID_TIER_DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
PAID_TIER_DEFAULT_MODEL = "gpt-6-luna"


def validate_paid_tier(llm: dict[str, Any]) -> None:
    if llm.get("tier", "local") not in {"local", "paid"}:
        raise ValueError("launcher settings llm.tier must be local or paid")
    for key in ("paid_base_url", "paid_model"):
        if not isinstance(llm.get(key, ""), str):
            raise ValueError(f"launcher settings llm.{key} must be a string")
    if llm.get("paid_base_url"):
        # The BHM gateway only talks to loopback/private endpoints (the local CPA proxy), so reject anything else early.
        from blackholememory.local_endpoint_policy import validate_local_endpoint

        try:
            validate_local_endpoint(llm["paid_base_url"])
        except Exception as exc:  # LocalEndpointError is not a ValueError; settings validation reports ValueError
            raise ValueError(f"launcher settings llm.paid_base_url must be a local endpoint: {exc}") from exc


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


_KEY_ENV = "BHM_PAID_LLM_API_KEY"


def paid_api_key_present() -> bool:
    """True when the key exists in this process or in the Windows user environment (never returns the key)."""

    if os.environ.get(_KEY_ENV):
        return True
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            return bool(winreg.QueryValueEx(key, _KEY_ENV)[0])
    except (ImportError, OSError):
        return False


def store_paid_api_key(key: str, *, runner: Any = subprocess.run) -> None:
    """Save the key in the Windows user environment, where the BHM API start script adopts it.

    It is deliberately not written to launcher settings or logs, and it reaches PowerShell through the
    child's environment instead of the command line.
    """

    key = key.strip()
    if not key or any(ch in key for ch in ("\r", "\n", "\0")):
        raise ValueError("the API key is empty or contains control characters")
    runner(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command",
         f"[Environment]::SetEnvironmentVariable('{_KEY_ENV}', $env:BHM_TMP_PAID_KEY, 'User')"],
        env={**os.environ, "BHM_TMP_PAID_KEY": key},
        check=True, timeout=30, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    os.environ[_KEY_ENV] = key
