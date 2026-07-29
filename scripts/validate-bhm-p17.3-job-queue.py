"""Run a deterministic P17.3 durable local-LLM queue lifecycle drill."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_ROOT))

from blackholememory.llm_job_queue import LLMJobQueue  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="bhm-p17.3-") as temporary:
        path = Path(temporary) / "queue.sqlite3"
        queue = LLMJobQueue(path, capacity=4)
        first = queue.enqueue(
            idempotency_key="p17.3-restartable",
            job_type="memory-summary",
            payload={"chunk": 1},
            priority=10,
            max_attempts=2,
            available_at=0,
        )
        duplicate = queue.enqueue(
            idempotency_key="p17.3-restartable",
            job_type="memory-summary",
            payload={"chunk": 1},
            priority=10,
            max_attempts=2,
            available_at=0,
        )
        claimed = queue.claim_next(owner="worker-old", lease_seconds=30, now=100)
        checkpoint = queue.checkpoint(
            claimed["job_id"],
            owner="worker-old",
            data={"offset": 1, "input_digest": hashlib.sha256(b"chunk-1").hexdigest()},
        )
        paused = queue.pause(reason="interactive activity")
        paused_claim = queue.claim_next(owner="worker-old", lease_seconds=30, now=100)
        queue.resume()

        reopened = LLMJobQueue(path, capacity=4)
        recovered_count = reopened.recover_processing(reason="simulated restart")
        recovered = reopened.claim_next(owner="worker-new", lease_seconds=30, now=time.time() + 1)
        terminal_status = reopened.fail(
            recovered["job_id"],
            owner="worker-new",
            error="deterministic validation failure",
            retryable=True,
            retry_delay_seconds=0,
        )
        cancelled = reopened.enqueue(
            idempotency_key="p17.3-cancelled",
            job_type="docs-pack",
            payload={"chunk": 2},
            priority=100,
            available_at=0,
        )
        cancelled_record = reopened.cancel(cancelled.job_id, reason="operator cancel")
        status = reopened.status()
        report = {
            "ok": bool(
                first.inserted
                and not duplicate.inserted
                and claimed["job_id"] == first.job_id
                and paused["paused"]
                and paused_claim is None
                and recovered_count == 1
                and recovered["checkpoint_digest"] == checkpoint.digest
                and terminal_status == "dead_letter"
                and cancelled_record["status"] == "cancelled"
                and status["pending"] == 0
            ),
            "schema_version": status["schema_version"],
            "idempotent_duplicate": not duplicate.inserted,
            "priority_claim": claimed["job_id"] == first.job_id,
            "pause_blocked_claim": paused_claim is None,
            "restart_recovered": recovered_count,
            "checkpoint_digest_present": bool(checkpoint.digest),
            "dead_letter_status": terminal_status,
            "cancel_status": cancelled_record["status"],
            "counts": status["counts"],
            "temporary_path": str(path),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
