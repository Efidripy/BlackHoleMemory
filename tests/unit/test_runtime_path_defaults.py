from __future__ import annotations

from pathlib import Path

from blackholememory import galaxy


REPO_ROOT = Path(__file__).resolve().parents[2]
LEGACY_LIVE_MEMORY_DEFAULTS = (
    REPO_ROOT / "scripts" / "bhm-code-graph.py",
    REPO_ROOT / "scripts" / "bhm-code-graph-query.py",
    REPO_ROOT / "scripts" / "bhm-conventions.py",
    REPO_ROOT / "scripts" / "bhm-repository-index.py",
    REPO_ROOT / "scripts" / "bhm_classify_projection_orphans.py",
    REPO_ROOT / "scripts" / "bhm_quarantine_projection_orphans.py",
    REPO_ROOT / "scripts" / "bhm_reconcile_projection.py",
    REPO_ROOT / "scripts" / "build-bhm-p22-live-graphs.py",
    REPO_ROOT / "scripts" / "promote-bhm-parser-v2.py",
    REPO_ROOT / "scripts" / "requeue-bhm-dead-letters.py",
    REPO_ROOT / "scripts" / "validate-bhm-p22-activation.py",
    REPO_ROOT / "scripts" / "validate-bhm-wi01-repository-index.py",
)


def test_galaxy_uses_canonical_runtime_dir() -> None:
    assert galaxy.LIVE_MEMORY_DIR == galaxy.settings.runtime_dir / "live-memory"
    assert galaxy.MEMORY_DATABASE_FILE == galaxy.settings.runtime_dir / "live-memory" / "memories.sqlite3"


def test_operator_live_memory_defaults_do_not_use_retired_runtime_directory() -> None:
    legacy_literals = (
        'REPO_ROOT / "runtime" / "live-memory"',
        'ROOT / "runtime" / "live-memory"',
    )
    for path in LEGACY_LIVE_MEMORY_DEFAULTS:
        source = path.read_text(encoding="utf-8")
        assert not any(literal in source for literal in legacy_literals), path
