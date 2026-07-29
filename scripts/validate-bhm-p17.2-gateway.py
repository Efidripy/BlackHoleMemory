"""Exercise the versioned Local LLM Gateway against the discovered local server."""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys


from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))
INVENTORY = REPO_ROOT / "scripts" / "validate-bhm-p17.1-llm-inventory.py"

from blackholememory.llm_gateway import GatewayRequest  # noqa: E402
from blackholememory.llm_gateway import LocalLLMGateway  # noqa: E402
from blackholememory.llm_gateway import LocalOpenAICompatibleAdapter  # noqa: E402
from blackholememory.llm_gateway import ModelDefinition  # noqa: E402
from blackholememory.llm_gateway import ModelRegistry  # noqa: E402
from blackholememory.llm_gateway import PromptDefinition  # noqa: E402
from blackholememory.llm_gateway import PromptRegistry  # noqa: E402


MIGRATED_LLM_PATHS = (
    REPO_ROOT / "src" / "blackholememory" / "agents" / "developer_agent.py",
    REPO_ROOT / "src" / "blackholememory" / "app.py",
    REPO_ROOT / "scripts" / "bhm_reflection_daemon.py",
)
DIRECT_LLM_ENDPOINT_RE = re.compile(r"chat/completions")


def load_inventory():
    spec = importlib.util.spec_from_file_location("bhm_p17_llm_inventory", INVENTORY)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load local inventory")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def scan_migrated_paths() -> list[dict[str, object]]:
    offenders: list[dict[str, object]] = []
    for path in MIGRATED_LLM_PATHS:
        for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if DIRECT_LLM_ENDPOINT_RE.search(line) and "BHM_PROVIDER_WARMUP_ENDPOINT" not in line:
                offenders.append({"path": str(path.relative_to(REPO_ROOT)), "line": line_number})
    return offenders


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()
    inventory = load_inventory()
    server = inventory.discover_llama_process()
    if server is None:
        print(json.dumps({"ok": False, "error": "local llama-server not found"}))
        return 1
    api_key = inventory._command_value(inventory._process_commandline(server["pid"]), "--api-key")
    model_id = server["model_path"]
    base_url = f"http://{server['host']}:{server['port']}/v1"
    prompts = PromptRegistry([PromptDefinition("gateway_probe", "1", "Return JSON with status=ok", output_mode="json")])
    models = ModelRegistry([ModelDefinition(model_id, base_url, frozenset({"json", "tools"}), api_key=api_key)])
    gateway = LocalLLMGateway(prompts=prompts, models=models, adapter=LocalOpenAICompatibleAdapter())
    result = gateway.complete(
        GatewayRequest(
            request_id="p17.2-live-probe",
            prompt_id="gateway_probe",
            model_id=model_id,
            messages=({"role": "user", "content": 'Return exactly {"status":"ok"}'},),
            max_tokens=32,
            json_required_keys=("status",),
            timeout_seconds=args.timeout,
        )
    )
    offenders = scan_migrated_paths()
    report = {
        "ok": bool(result.ok and not offenders),
        "gateway": gateway.snapshot(),
        "result": result.as_dict(),
        "local_only": True,
        "direct_call_scan": {"ok": not offenders, "offenders": offenders},
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
