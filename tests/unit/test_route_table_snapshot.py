"""FIX-017 safety net: the FastAPI route table must not change while app.py is split into modules.

Routes moved out of app.py are attached in groups, so the global registration order may change between
unrelated routes. What must hold: the same set of (methods, path, name, operation id, ...) routes, and no route
is shadowed by an earlier one (same method, overlapping path).
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

from starlette.routing import compile_path

ROOT = Path(__file__).resolve().parents[2]
SNAPSHOT = ROOT / "tests" / "fixtures" / "route-table.json"


def _current() -> list[dict]:
    spec = importlib.util.spec_from_file_location("dump_route_table", ROOT / "scripts" / "dump-bhm-route-table.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return json.loads(json.dumps(module.route_table()))


def _identity(route: dict) -> tuple:
    return (route["kind"], tuple(route["methods"]), route["path"], route["name"], route["operation_id"])


def _strip_index(route: dict) -> dict:
    return {key: value for key, value in route.items() if key != "index"}


def test_route_table_matches_the_frozen_snapshot_ignoring_order() -> None:
    expected = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    current = _current()
    assert sorted(map(_identity, current), key=repr) == sorted(map(_identity, expected), key=repr), "route set changed"
    by_identity = {_identity(route): _strip_index(route) for route in expected}
    changed = [route["path"] for route in current if _strip_index(route) != by_identity[_identity(route)]]
    assert not changed, (
        f"route fields drifted for {changed[:5]}; if intentional regenerate tests/fixtures/route-table.json "
        "with scripts/dump-bhm-route-table.py --out"
    )


def test_no_route_is_shadowed_by_an_earlier_overlapping_route() -> None:
    routes = [route for route in _current() if route["path"] and route["methods"]]
    for position, earlier in enumerate(routes):
        pattern, _, _ = compile_path(earlier["path"])
        for later in routes[position + 1 :]:
            if earlier["path"] == later["path"] or not set(earlier["methods"]) & set(later["methods"]):
                continue
            sample = re.sub(r"\{[^}]+\}", "x", later["path"])
            assert not pattern.match(sample), f"{later['path']} is shadowed by earlier {earlier['path']}"
