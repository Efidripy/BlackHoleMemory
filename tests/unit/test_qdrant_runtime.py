from __future__ import annotations

from pathlib import Path

from blackholememory.qdrant_runtime import QDRANT_IMAGE_REF
from blackholememory.qdrant_runtime import validate_qdrant_runtime


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_qdrant_runtime_contract_is_pinned_and_loopback_only():
    compose = f'''
services:
  qdrant:
    image: {QDRANT_IMAGE_REF}
    ports:
      - "127.0.0.1:${{BHM_QDRANT_HTTP_PORT:-6333}}:6333"
      - "127.0.0.1:${{BHM_QDRANT_GRPC_PORT:-6334}}:6334"
'''

    launcher = f'QDRANT_IMAGE = "{QDRANT_IMAGE_REF}"\n["docker", "pull", QDRANT_IMAGE]'
    report = validate_qdrant_runtime(compose, launcher_text=launcher)

    assert report["ok"] is True
    assert all(report["checks"].values())


def test_checked_in_qdrant_compose_and_launcher_stay_in_sync():
    report = validate_qdrant_runtime(
        (REPO_ROOT / "infra" / "qdrant" / "docker-compose.yml").read_text(encoding="utf-8"),
        launcher_text=(REPO_ROOT / "scripts" / "bhm_launcher.py").read_text(encoding="utf-8"),
    )

    assert report["ok"] is True


def test_qdrant_runtime_contract_rejects_latest_and_wildcard_binding():
    compose = '''
services:
  qdrant:
    image: qdrant/qdrant:latest
    ports:
      - "6333:6333"
      - "0.0.0.0:6334:6334"
'''

    report = validate_qdrant_runtime(compose, launcher_text="qdrant/qdrant:latest")

    assert report["ok"] is False
    assert report["checks"]["image_pinned"] is False
    assert report["checks"]["loopback_bindings"] is False
    assert report["checks"]["no_latest_image"] is False
