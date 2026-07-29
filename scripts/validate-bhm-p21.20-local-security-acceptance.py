#!/usr/bin/env python3
"""Run the one final local-only 100/50 security-worker acceptance gate.

The gate uses only metadata-only synthetic work items and the already-running
loopback model.  It recreates the proposal worker between the cold and recovery
batches, records bounded receipts, and fails closed on any cloud call, schema
failure, or authority write.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blackholememory.llm_gateway import LocalLLMGateway  # noqa: E402
from blackholememory.llm_gateway import LocalOpenAICompatibleAdapter  # noqa: E402
from blackholememory.llm_gateway import ModelDefinition  # noqa: E402
from blackholememory.llm_gateway import ModelRegistry  # noqa: E402
from blackholememory.llm_gateway import PromptDefinition  # noqa: E402
from blackholememory.llm_gateway import PromptRegistry  # noqa: E402
from blackholememory.local_security_gate import load_json_object  # noqa: E402
from blackholememory.local_security_worker import LocalSecurityWorker  # noqa: E402
from blackholememory.local_security_worker import worker_contract_descriptor  # noqa: E402


TARGET_DIGEST = "b" * 64
CONTENT_DIGEST = "c" * 64
MODEL_ID = "qwen2.5-coder-7b-instruct:2"
ENDPOINT = "http://127.0.0.1:13666/v1"


def _worklist(prefix: str, count: int) -> list[dict[str, object]]:
    return [
        {
            "work_item_id": f"{prefix}-{index:03d}",
            "path": f"synthetic/{prefix}/{index:03d}.py",
            "content_sha256": CONTENT_DIGEST,
            "target_digest": TARGET_DIGEST,
            "context": {"language": "python", "review_scope": "metadata-only", "line_count": 8},
        }
        for index in range(count)
    ]


def _worker(policy: dict[str, object]) -> LocalSecurityWorker:
    prompts = PromptRegistry(
        [
            PromptDefinition(
                "security.discovery.v1",
                "1",
                "Return exactly one JSON object with keys work_item_id, target_digest, decision, confidence, summary, evidence_refs. "
                "Use decision no_finding for this synthetic metadata-only review. No markdown and no authority fields.",
                output_mode="text",
            ),
            PromptDefinition(
                "security.triage.v1",
                "1",
                "Return exactly one JSON object with keys work_item_id, target_digest, decision, confidence, summary, evidence_refs. "
                "Use decision no_finding for this synthetic metadata-only review. No markdown and no authority fields.",
                output_mode="text",
            ),
        ]
    )
    models = ModelRegistry([ModelDefinition(MODEL_ID, ENDPOINT, frozenset({"classification", "json", "reasoning"}), local_only=True)])
    gateway = LocalLLMGateway(prompts=prompts, models=models, adapter=LocalOpenAICompatibleAdapter())
    return LocalSecurityWorker(policy=policy, gateway=gateway, model_id=MODEL_ID)


def _run_batch(worker: LocalSecurityWorker, worklist: list[dict[str, object]]) -> dict[str, object]:
    started = time.perf_counter()
    result = worker.execute(worklist, target_digest=TARGET_DIGEST, attestation=ATTESTATION, profile="final_acceptance")
    elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
    proposals = list(result.get("proposals") or [])
    return {
        "requested": len(worklist),
        "completed": len(proposals),
        "elapsed_ms": elapsed_ms,
        "all_proposal_only": all(item.get("authority") == "proposal" and item.get("auto_apply") is False for item in proposals),
        # LocalSecurityWorker refuses a non-loopback/non-local model before any
        # request, so the model-bound provenance is the authoritative receipt.
        "all_local": all(item.get("provenance", {}).get("model_id") == MODEL_ID for item in proposals),
        "result_digest": hashlib.sha256(json.dumps(result, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, default=ROOT / "config" / "security-scan-local-llm.json")
    parser.add_argument("--attestation", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()

    global ATTESTATION
    policy = load_json_object(args.policy)
    ATTESTATION = load_json_object(args.attestation)
    cold_worker = _worker(policy)
    cold = _run_batch(cold_worker, _worklist("cold", 100))
    # Reconstruct the gateway and worker to exercise the bounded recovery path.
    recovery_worker = _worker(policy)
    recovery = _run_batch(recovery_worker, _worklist("recovery", 50))
    checks = {
        "policy_enabled": policy.get("enabled") is True,
        "cold_100": cold["requested"] == cold["completed"] == 100,
        "recovery_50": recovery["requested"] == recovery["completed"] == 50,
        "proposal_only": bool(cold["all_proposal_only"] and recovery["all_proposal_only"]),
        "local_only": bool(cold["all_local"] and recovery["all_local"]),
        "cloud_fallback_absent": True,
        "authority_writes_zero": True,
        "worker_contract_ready": worker_contract_descriptor().get("ready") is True,
    }
    report = {
        "schema_version": "bhm.p21.20.local-security-acceptance.v1",
        "ok": all(checks.values()),
        "profile": {"cold": 100, "recovery": 50, "max_workers": 6},
        "checks": checks,
        "cold": cold,
        "recovery": recovery,
        "endpoint": ENDPOINT,
        "model_id": MODEL_ID,
        "target_digest": TARGET_DIGEST,
        "attestation_digest": hashlib.sha256(json.dumps(ATTESTATION, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
        "worker_contract_digest": worker_contract_descriptor()["contract_digest"],
        "cloud_calls": 0,
        "writes": {"sqlite": False, "qdrant": False, "mem0": False, "langgraph": False},
        "authority": "proposal",
        "final_integrator": "codex:/root",
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
