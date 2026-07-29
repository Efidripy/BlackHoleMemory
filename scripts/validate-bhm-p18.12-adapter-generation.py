"""Live and fixture gate for P18.12 MCP adapter generation."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
GENERATOR = REPO_ROOT / "scripts" / "generate-bhm-mcp-adapters.py"
MANIFEST = REPO_ROOT / "config" / "mcp-registration.json"


def _hash(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _run(*args: str) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(GENERATOR), *args, "--json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"adapter generator returned invalid JSON: {completed.stdout[-500:]}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("adapter generator returned a non-object payload")
    payload["returncode"] = completed.returncode
    if completed.stderr.strip():
        payload["stderr_present"] = True
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--skip-live", action="store_true", help="run canary only")
    args = parser.parse_args()

    targets = [
        Path(__import__("os").environ.get("USERPROFILE", "")) / ".codex" / "config.toml",
        Path(__import__("os").environ.get("USERPROFILE", "")) / ".claude" / "settings.json",
    ]
    before = {str(path): _hash(path) for path in targets}
    canary = _run("--manifest", str(args.manifest), "--canary")
    after_canary = {str(path): _hash(path) for path in targets}
    live = None if args.skip_live else _run("--manifest", str(args.manifest), "--check")
    after_live = {str(path): _hash(path) for path in targets}
    live_unchanged = before == after_canary == after_live
    client_count = len(canary.get("clients", []))
    result = {
        "schema": "bhm.mcp.adapter-generation-validation.v1",
        "ok": bool(canary.get("ok") is True and canary.get("writes_live_state") is False and canary.get("rollback", {}).get("ok") is True and client_count == 2 and live_unchanged and (live is None or live.get("ok") is True)),
        "canary": canary,
        "live_check": live,
        "live_targets_unchanged": live_unchanged,
        "writes_live_state": False,
        "target_hashes": {"before": before, "after": after_live},
    }
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
