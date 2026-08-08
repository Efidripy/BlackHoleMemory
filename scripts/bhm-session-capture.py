"""Explicit WI-05 session capture and progressive-disclosure CLI."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from blackholememory.filesystem_boundaries import replace_bytes_safely
from blackholememory.session_capture import DISCLOSURE_LEVELS
from blackholememory.session_capture import SESSION_CAPTURE_SCHEMA_VERSION
from blackholememory.session_capture import SessionCaptureError
from blackholememory.session_capture import build_session_capture_preview


def _emit(value: object, report: str | None = None) -> None:
    rendered = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    print(rendered)
    if report:
        target = Path(report).expanduser()
        replace_bytes_safely(target, (rendered + "\n").encode("utf-8"))


def _fixture(path: str | None) -> dict[str, list[dict]]:
    if not path:
        return {"observations": [], "session_records": [], "memories": []}
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SessionCaptureError("fixture must contain observations/session_records/memories")
    return {
        key: [dict(item) for item in list(payload.get(key) or []) if isinstance(item, dict)]
        for key in ("observations", "session_records", "memories")
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--action", choices=("plan", "preview"), default="preview")
    parser.add_argument("--fixture")
    parser.add_argument("--project", default="blackholememory")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--disclosure", choices=DISCLOSURE_LEVELS, default="standard")
    parser.add_argument("--token-budget", type=int, default=1_200)
    parser.add_argument("--max-items", type=int, default=32)
    parser.add_argument("--stale-days", type=int, default=90)
    parser.add_argument("--undo-window-seconds", type=int, default=900)
    parser.add_argument("--now", default="")
    parser.add_argument("--report")
    args = parser.parse_args()
    try:
        fixture = _fixture(args.fixture)
        if args.action == "plan":
            _emit(
                {
                    "schema_version": SESSION_CAPTURE_SCHEMA_VERSION,
                    "ok": True,
                    "action": "plan",
                    "project": args.project,
                    "session_id": args.session_id or None,
                    "disclosure_levels": list(DISCLOSURE_LEVELS),
                    "writes_sqlite": False,
                    "writes_mem0": False,
                    "writes_qdrant": False,
                    "model_started": False,
                },
                args.report,
            )
            return 0
        clock = datetime.fromisoformat(args.now.replace("Z", "+00:00")) if args.now else None
        result = build_session_capture_preview(
            fixture["observations"],
            session_records=fixture["session_records"],
            memories=fixture["memories"],
            project=args.project,
            session_id=args.session_id,
            disclosure=args.disclosure,
            token_budget=args.token_budget,
            max_items=args.max_items,
            stale_days=args.stale_days,
            undo_window_seconds=args.undo_window_seconds,
            now=clock,
        )
        _emit(result, args.report)
        return 0
    except (SessionCaptureError, OSError, ValueError, json.JSONDecodeError) as exc:
        _emit(
            {
                "schema_version": SESSION_CAPTURE_SCHEMA_VERSION,
                "ok": False,
                "error": type(exc).__name__,
                "detail": str(exc)[:1_000],
            },
            args.report,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
