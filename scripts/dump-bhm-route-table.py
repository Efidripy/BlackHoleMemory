"""Dump the FastAPI route table of ``blackholememory.app`` as deterministic JSON.

One item per route, in registration order. Used as a safety net while ``app.py`` is
split into routers: the table must not change. Does not start a server.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def _model_name(model: Any) -> str | None:
    if model is None:
        return None
    return f"{getattr(model, '__module__', '')}.{getattr(model, '__qualname__', repr(model))}"


def _endpoint_name(endpoint: Any) -> str | None:
    if endpoint is None:
        return None
    target = endpoint if hasattr(endpoint, "__qualname__") else type(endpoint)
    return f"{target.__module__}:{target.__qualname__}"


def route_table() -> list[dict[str, Any]]:
    from blackholememory.app import app

    items: list[dict[str, Any]] = []
    for index, route in enumerate(app.routes):
        endpoint = getattr(route, "endpoint", None)
        methods = getattr(route, "methods", None)
        item: dict[str, Any] = {
            "index": index,
            "kind": type(route).__name__,
            "path": getattr(route, "path", None),
            "methods": sorted(methods) if methods else [],
            "name": getattr(route, "name", None),
            "operation_id": None,
            "tags": list(getattr(route, "tags", None) or []),
            "response_model": _model_name(getattr(route, "response_model", None)),
            "include_in_schema": bool(getattr(route, "include_in_schema", True)),
            "endpoint": _endpoint_name(endpoint),
        }
        if hasattr(route, "unique_id"):
            item["operation_id"] = getattr(route, "operation_id", None) or route.unique_id
        items.append(item)
    return items


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="write JSON here instead of stdout")
    args = parser.parse_args()
    text = json.dumps(
        [dict(sorted(item.items())) for item in route_table()], indent=2, ensure_ascii=False, sort_keys=True
    )
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
