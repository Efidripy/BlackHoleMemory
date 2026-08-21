"""Run bounded WL-295.2 freshness candidate detection and operator actions."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from blackholememory.freshness_candidates import decide_freshness_candidate, detect_freshness_candidates, list_freshness_candidates, upsert_freshness_candidates  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--as-of")
    parser.add_argument("--age-days", type=int, default=30)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--action", choices=("scan", "list", "dismissed", "accepted"), default="scan")
    parser.add_argument("--candidate-id")
    parser.add_argument("--decision-note")
    parser.add_argument("--caller-ref")
    parser.add_argument("--idempotency-key")
    args = parser.parse_args()
    if args.action == "scan":
        from datetime import UTC, datetime
        as_of = args.as_of or datetime.now(UTC).isoformat().replace("+00:00", "Z")
        candidates = detect_freshness_candidates(args.database, project=args.project, as_of=as_of, age_days=args.age_days, limit=args.limit)
        result = {"candidates": candidates, "persistence": upsert_freshness_candidates(args.database, candidates, observed_at=as_of), "automatic_mutations": 0}
    elif args.action == "list":
        result = {"candidates": list_freshness_candidates(args.database, project=args.project, limit=args.limit), "automatic_mutations": 0}
    else:
        required = (args.candidate_id, args.decision_note, args.caller_ref, args.idempotency_key)
        if any(value is None for value in required):
            parser.error("decision actions require --candidate-id, --decision-note, --caller-ref and --idempotency-key")
        result = decide_freshness_candidate(args.database, project=args.project, candidate_id=args.candidate_id, action=args.action, decision_note=args.decision_note, caller_ref=args.caller_ref, idempotency_key=args.idempotency_key)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
