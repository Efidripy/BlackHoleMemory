from __future__ import annotations

import json
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest


# Resolve the launcher beside its helpers instead of an unrelated installed
# `scripts` package on developer machines.
# ruff: noqa: E402
SCRIPTS_ROOT = Path(__file__).resolve().parents[2] / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import bhm_launcher as launcher


BOOTSTRAP_TOKEN = "bootstrap-token-00000000000000000000+/="
CALLER_TOKEN = "caller-token-000000000000000000000000"


def test_launcher_local_state_stays_under_dot_runtime() -> None:
    assert launcher.LAUNCHER_LOG_DIR == launcher.PROJECT_ROOT / ".runtime" / "logs" / "launcher"
    assert launcher.LAUNCHER_SETTINGS_BACKUP_DIR == (
        launcher.PROJECT_ROOT / ".runtime" / "logs" / "launcher" / "config-backups"
    )


def test_mcp_integration_status_uses_current_http_contract(tmp_path, monkeypatch) -> None:
    appdata = tmp_path / "AppData"
    target = appdata / "Claude" / "claude_desktop_config.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps(launcher.mcp_config_payload()), encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "Home")

    ok, detail = launcher.mcp_integration_status()

    assert ok is True
    assert str(target) in detail


class _JsonResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def test_bhm_human_ui_detection_is_origin_and_path_bounded() -> None:
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}/bhm/galaxy") is True
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}/") is True
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}/docs") is False
    assert launcher._is_bhm_human_ui_url(f"{launcher.BHM_BASE_URL}.example.invalid/bhm/galaxy") is False
    assert launcher._is_bhm_human_ui_url("not a URL") is False


def test_mint_uses_authenticated_post_with_a_bounded_timeout(monkeypatch) -> None:
    calls: list[tuple[str, dict, float]] = []

    def fake_post(url: str, payload: dict, timeout: float) -> dict:
        calls.append((url, payload, timeout))
        return {"bootstrap_token": BOOTSTRAP_TOKEN}

    monkeypatch.setattr(launcher, "post_json", fake_post)

    assert launcher.mint_bhm_ui_bootstrap_token() == BOOTSTRAP_TOKEN
    assert calls == [
        (
            f"{launcher.BHM_BASE_URL}/bhm/ui/session/mint",
            {"project": None},
            launcher.UI_SESSION_MINT_TIMEOUT_SECONDS,
        )
    ]


def test_open_galaxy_mints_before_open_and_only_places_bootstrap_in_fragment() -> None:
    events: list[str] = []
    opened: list[str] = []

    def mint() -> str:
        events.append("mint")
        return BOOTSTRAP_TOKEN

    def opener(target: str) -> bool:
        events.append("open")
        opened.append(target)
        return True

    launcher.open_launcher_link(
        f"{launcher.BHM_BASE_URL}/bhm/galaxy?project=BlackHoleMemory",
        mint=mint,
        opener=opener,
    )

    assert events == ["mint", "open"]
    assert len(opened) == 1
    target = urlsplit(opened[0])
    assert target.path == "/bhm/galaxy"
    assert target.query == "project=BlackHoleMemory"
    assert parse_qs(target.fragment) == {launcher.BHM_UI_BOOTSTRAP_FRAGMENT_KEY: [BOOTSTRAP_TOKEN]}
    assert CALLER_TOKEN not in opened[0]


def test_scoped_launcher_adds_default_project_to_galaxy(monkeypatch) -> None:
    monkeypatch.setattr(launcher, "_read_process_or_user_env_value", lambda key: {
        "BHM_CALLER_PROJECTS": "blackholememory",
        "BHM_CALLER_DEFAULT_PROJECT": "blackholememory",
    }.get(key))

    target = launcher.browser_target_for_url(
        f"{launcher.BHM_BASE_URL}/bhm/galaxy",
        mint=lambda: BOOTSTRAP_TOKEN,
    )
    parsed = urlsplit(target)
    assert parsed.query == "project=blackholememory"


def test_non_bhm_links_open_without_minting() -> None:
    minted: list[bool] = []
    opened: list[str] = []
    url = launcher.endpoint_url("qdrant_http", "/dashboard/")

    launcher.open_launcher_link(
        url,
        mint=lambda: minted.append(True) or BOOTSTRAP_TOKEN,
        opener=lambda target: opened.append(target) or True,
    )

    assert minted == []
    assert opened == [url]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"bootstrap_token": None},
        {"bootstrap_token": "too-short"},
        {"bootstrap_token": f" {'x' * 32}"},
        {"bootstrap_token": "x" * 257},
    ],
)
def test_invalid_mint_response_fails_closed_before_browser_open(monkeypatch, payload: dict) -> None:
    opened: list[str] = []
    monkeypatch.setattr(launcher, "post_json", lambda *_args, **_kwargs: payload)

    with pytest.raises(launcher.LauncherUiSessionError, match="response was invalid"):
        launcher.open_launcher_link(
            f"{launcher.BHM_BASE_URL}/bhm/galaxy",
            opener=lambda target: opened.append(target) or True,
        )

    assert opened == []


def test_post_json_keeps_caller_credential_in_authorization_header(monkeypatch) -> None:
    captured: list[object] = []
    monkeypatch.setattr(launcher, "_required_bhm_caller_token", lambda: CALLER_TOKEN)

    def fake_urlopen(request, timeout: float):
        captured.extend([request, timeout])
        return _JsonResponse(b'{"ok": true}')

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)

    assert launcher.post_json(f"{launcher.BHM_BASE_URL}/bhm/ui/session/mint", {}) == {"ok": True}
    request = captured[0]
    assert request.get_header("Authorization") == f"Bearer {CALLER_TOKEN}"
    assert CALLER_TOKEN not in request.full_url
    assert CALLER_TOKEN.encode() not in request.data


def test_health_probe_remains_anonymous(monkeypatch) -> None:
    captured: list[object] = []

    def fake_urlopen(request, timeout: float):
        captured.extend([request, timeout])
        return _JsonResponse(b"")

    monkeypatch.setattr(launcher.urllib.request, "urlopen", fake_urlopen)

    status = launcher.http_status(launcher.BHM_API_HEALTH_URL)

    assert status.state == "Running"
    request = captured[0]
    assert request.get_header("Authorization") is None
    assert request.get_header("User-agent") == "BHM-Control-Deck"
