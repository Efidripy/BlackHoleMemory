"""FIX-017 safety net: the FastAPI route table must not change while app.py is split into routers."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests" / "fixtures" / "route-table.json"


def _current() -> list[dict]:
    spec = importlib.util.spec_from_file_location("dump_route_table", ROOT / "scripts" / "dump-bhm-route-table.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return json.loads(json.dumps(module.route_table()))


def test_route_table_matches_the_frozen_snapshot() -> None:
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = _current()
    assert [r["path"] for r in current] == [r["path"] for r in expected], "route order or paths changed"
    assert current == expected, (
        "route table drifted; if the change is intentional regenerate tests/fixtures/route-table.json "
        "with scripts/dump-bhm-route-table.py --out"
    )
