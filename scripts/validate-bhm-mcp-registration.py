"""Executable P18.1 single-owner MCP registration gate."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

from blackholememory.mcp_registration import RegistrationContractError
from blackholememory.mcp_registration import canonical_fixture
from blackholememory.mcp_registration import evaluate_registrations
from blackholememory.mcp_registration import load_contract
from blackholememory.mcp_registration import load_registrations


REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = REPO_ROOT / "config" / "mcp-registration.json"


def _default_codex_config() -> Path:
    return Path(os.environ.get("USERPROFILE", "")) / ".codex" / "config.toml"


def _default_surfaces() -> list[Path]:
    return [Path(os.environ.get("USERPROFILE", "")) / ".claude" / "settings.json"]


def run_self_test(contract_path: Path) -> dict[str, Any]:
    contract = load_contract(contract_path, repo_root=REPO_ROOT)
    return evaluate_registrations(contract, [canonical_fixture(contract)])


def run_live(contract_path: Path, codex_config: Path, surfaces: list[Path]) -> dict[str, Any]:
    contract = load_contract(contract_path, repo_root=REPO_ROOT)
    registrations = []
    sources: list[dict[str, str]] = []
    paths = [codex_config, *surfaces]
    for index, path in enumerate(paths):
        if not path.exists():
            sources.append({"path": str(path), "status": "missing"})
            continue
        registrations.extend(
            load_registrations(
                path,
                client="codex" if index == 0 else "claude",
                repo_root=REPO_ROOT,
                workspace_root=REPO_ROOT.parent.parent / "workspace",
                user_root=Path(os.environ.get("USERPROFILE", "")),
                default_base_url=contract.default_base_url,
                default_mcp_url=str(contract.canonical.get("url") or ""),
            )
        )
        sources.append({"path": str(path), "status": "scanned"})
    result = evaluate_registrations(contract, registrations)
    result["mode"] = "live-read-only"
    result["sources"] = sources
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--contract", type=Path, default=CONTRACT_PATH)
    parser.add_argument("--live", action="store_true", help="scan Codex and explicitly supplied JSON surfaces read-only")
    parser.add_argument("--codex-config", type=Path, default=_default_codex_config())
    parser.add_argument("--json-surface", action="append", type=Path, default=[])
    args = parser.parse_args()
    try:
        result = run_live(args.contract, args.codex_config, args.json_surface or _default_surfaces()) if args.live else run_self_test(args.contract)
    except RegistrationContractError as exc:
        result = {"ok": False, "fail_closed": True, "error": str(exc), "writes_live_state": False}
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result.get("ok") is True else 1


if __name__ == "__main__":
    raise SystemExit(main())
