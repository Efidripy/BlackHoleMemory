from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
HTTP_READINESS_PROBE = "GET /healthz HTTP/1.1"


def test_qdrant_compose_healthcheck_requires_http_readiness() -> None:
    compose = (ROOT / "infra" / "qdrant" / "docker-compose.yml").read_text(encoding="utf-8")

    assert HTTP_READINESS_PROBE in compose
    assert "/dev/tcp/127.0.0.1/6333" in compose
    assert "$$status" in compose
    assert '"pidof", "qdrant"' not in compose


def test_ci_qdrant_service_uses_the_same_http_readiness_boundary() -> None:
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

    assert HTTP_READINESS_PROBE in workflow
    assert "/dev/tcp/127.0.0.1/6333" in workflow
    assert '--health-cmd "pidof qdrant"' not in workflow
