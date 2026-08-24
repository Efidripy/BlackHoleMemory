from __future__ import annotations

import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "validate-bhm-semantic-quality.py"
SPEC = importlib.util.spec_from_file_location("bhm_p28_wi82_semantic_quality", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _status_fixture(*, completed_at: str, parser_errors: int = 0) -> tuple[dict, dict, dict]:
    status = {
        "contract_digest": "contract",
        "index": {
            "fresh": True,
            "current_snapshot": {
                "snapshot_id": "snapshot-1",
                "snapshot_digest": "snapshot-digest",
                "completed_at": completed_at,
            },
            "latest_job": {"completed_at": completed_at},
        },
        "graph": {
            "status": "completed",
            "graph_snapshot_id": "graph-1",
            "repository_snapshot_id": "snapshot-1",
            "summary": {"parser_error_count": parser_errors},
        },
    }
    coverage = {
        "index_fresh": True,
        "coverage": {"complete": parser_errors == 0, "errors": parser_errors},
    }
    schema = {"graph_snapshot_id": "graph-1", "contract_digest": "contract"}
    return status, coverage, schema


def test_bounded_semantic_benchmark_is_deterministic_and_metadata_only(monkeypatch):
    monkeypatch.setenv("BHM_CODE_SEMANTIC_FUSION", "off")
    first = MODULE.run_semantic_benchmark(8)
    second = MODULE.run_semantic_benchmark(8)

    assert first["ok"] is True
    assert first["semantic_weight"] == 0.7
    assert first["semantic_weight_bounded"] is True
    assert first["top1_accuracy"] == 1.0
    assert first["baseline_top1_accuracy"] == 0.0
    assert first["mean_reciprocal_rank"] == 1.0
    assert first["ndcg_at_2"] == 1.0
    assert first["rank_improvement_count"] == 8
    assert first["metadata_only"] is True
    assert first["provider_calls"] == 0
    assert first["writes_sqlite_state"] is False
    assert first["writes_qdrant"] is False
    assert first["raw_source_returned"] is False
    assert first["digest"] == second["digest"]
    assert os.environ["BHM_CODE_SEMANTIC_FUSION"] == "off"


def test_freshness_evidence_accepts_aligned_fresh_snapshot():
    completed = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    status, coverage, schema = _status_fixture(completed_at=completed)
    result = MODULE._freshness_evidence(
        status,
        coverage,
        schema,
        max_age_seconds=60,
    )

    assert result["ok"] is True
    assert result["snapshot_graph_aligned"] is True
    assert result["parser_errors"] == 0


def test_freshness_evidence_fails_stale_or_parse_error_snapshot():
    stale = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    status, coverage, schema = _status_fixture(completed_at=stale)
    stale_result = MODULE._freshness_evidence(status, coverage, schema, max_age_seconds=60)
    assert stale_result["ok"] is False
    assert stale_result["age_within_budget"] is False

    completed = (datetime.now(timezone.utc) - timedelta(seconds=2)).isoformat()
    status, coverage, schema = _status_fixture(completed_at=completed, parser_errors=1)
    error_result = MODULE._freshness_evidence(status, coverage, schema, max_age_seconds=60)
    assert error_result["ok"] is False
    assert error_result["parser_errors"] == 1


class _FakeClient:
    def __init__(self, payloads: dict[str, dict]):
        self.payloads = payloads

    def get(self, path: str) -> dict:
        return self.payloads[path]


def test_runtime_health_keeps_provider_and_slo_separate():
    client = _FakeClient(
        {
            "/bhm/health": {
                "status": "healthy",
                "version": "test",
                "memory_store": {"backend": "sqlite-authoritative", "ready": True},
            },
            "/health/cutover": {
                "ok": True,
                "mem0": {"status": "projection-only", "direct_vector_writes": False},
            },
            "/bhm/health/slo": {
                "status": "breached",
                "observed": {"provider_ready": True, "qdrant_healthy": True, "projection_pending": 2, "projection_failed": 0},
                "checks": {"provider_ready": True, "qdrant_healthy": True, "projection_pending_within_budget": False},
            },
        }
    )

    result = MODULE._runtime_health(client)
    assert result["provider"]["ok"] is True
    assert result["authority"]["ok"] is True
    assert result["slo"]["ok"] is False
    assert result["ok"] is False


def test_script_declares_read_only_boundary_and_no_runtime_mutation():
    text = SCRIPT.read_text(encoding="utf-8").lower()
    for marker in (
        "writes_sqlite_state",
        "writes_qdrant",
        "model_started",
        "autonomous_apply",
        "raw_source_returned",
        "sqlite-authoritative",
        "qdrant-projection-only",
    ):
        assert marker in text
    for forbidden in ("upsert", "delete_collection", "run-bhm-projection-worker", "subprocess.run"):
        assert forbidden not in text


def test_live_client_rejects_non_local_target():
    try:
        MODULE._LocalRuntimeClient("https://example.invalid", "secret")
    except ValueError as exc:
        assert "local BHM runtime" in str(exc)
    else:
        raise AssertionError("external runtime target must be rejected")


def test_live_client_uses_local_bounded_transport(monkeypatch):
    calls: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit: int) -> bytes:
            calls["limit"] = limit
            return b'{"status":"healthy"}'

    def fake_open(request, *, timeout):
        calls["url"] = request.full_url
        calls["timeout"] = timeout
        return Response()

    monkeypatch.setattr(MODULE, "open_local_url", fake_open)
    result = MODULE._LocalRuntimeClient("http://127.0.0.1:8000", "t" * 32).get("/bhm/health")
    assert result == {"status": "healthy"}
    assert calls == {
        "url": "http://127.0.0.1:8000/bhm/health",
        "timeout": 20.0,
        "limit": MODULE.MAX_RESPONSE_BYTES + 1,
    }


def test_live_client_wraps_oversized_response_as_runtime_error(monkeypatch):
    class Oversized:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit: int) -> bytes:
            return b"x" * limit

    monkeypatch.setattr(MODULE, "open_local_url", lambda *_args, **_kwargs: Oversized())
    with pytest.raises(RuntimeError, match="bounded limit"):
        MODULE._LocalRuntimeClient("http://127.0.0.1:8000", "t" * 32).get("/bhm/health")
