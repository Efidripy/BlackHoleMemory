"""Fresh-lesson retrieval check: live ranks, and offline replay of PROTECT_RANK variants.

Usage (token in BHM_CALLER_TOKEN, service on 127.0.0.1:8000):
  python -X utf8 scripts/bhm_fresh_lessons_eval.py live
  python -X utf8 scripts/bhm_fresh_lessons_eval.py fetch --out pool.json
  python -X utf8 scripts/bhm_fresh_lessons_eval.py replay --pool pool.json [--variants 0,1,3,5,10]

`live` prints @1/@3/@10/MRR over tests/fixtures/fresh_lessons_paraphrases.json (rank of the expected id in the top 15).
`fetch` stores /bhm/search limit=100 detail=full candidates for those queries and for the golden set.
`replay` recomputes fused = rrf_score / current_multiplier x new multiplier, then MMR (lambda 0.94) per
BHM_CHECKPOINT_PROTECT_RANK variant. It ignores LLM-rerank and cap_checkpoints_in_top, so it only compares variants
with each other; absolute numbers are lower than live.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
PARAPHRASES = ROOT / "tests" / "fixtures" / "fresh_lessons_paraphrases.json"
GOLDEN = Path(os.environ.get("BHM_GOLDEN_PATH") or ROOT / ".docs" / "ops" / "memory-golden-v1.json")
BASE_URL = os.environ.get("BHM_BASE_URL", "http://127.0.0.1:8000")
PROJECT = "blackholememory"


def search(query: str, project: str = PROJECT, limit: int = 15, detail: str = "full") -> dict:
    token = os.environ.get("BHM_CALLER_TOKEN", "")
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "X-BHM-Caller-Surface": "launcher"}
    if project:
        headers["X-BHM-Caller-Project"] = project
    body = json.dumps({"query": query, "project": project, "limit": limit, "detail": detail}).encode("utf-8")
    started = time.perf_counter()
    with urllib.request.urlopen(urllib.request.Request(f"{BASE_URL}/bhm/search", data=body, headers=headers, method="POST"), timeout=90) as response:
        data = json.loads(response.read().decode("utf-8"))
    data["_ms"] = (time.perf_counter() - started) * 1000
    return data


def load_sets() -> dict[str, list[tuple[str, str, str]]]:
    raw = json.loads(PARAPHRASES.read_text(encoding="utf-8"))["sets"]
    return {name: [(g["group"], g["expect_id"], q) for g in groups for q in g["queries"]] for name, groups in raw.items()}


def metrics(ranks: list[int | None]) -> dict:
    n = len(ranks)
    return {
        "n": n,
        "@1": sum(r == 1 for r in ranks),
        "@3": sum(1 for r in ranks if r and r <= 3),
        "@10": sum(1 for r in ranks if r and r <= 10),
        "miss": sum(r is None for r in ranks),
        "MRR": round(sum(1 / r for r in ranks if r) / max(n, 1), 3),
    }


def cmd_live(_: argparse.Namespace) -> None:
    for name, rows in load_sets().items():
        ranks = []
        for group, expect_id, query in rows:
            response = search(query)
            ids = [m["id"] for m in response["memories"]]
            rank = ids.index(expect_id) + 1 if expect_id in ids else None
            ranks.append(rank)
            print(f"  {name}/{group} rank={rank} {round(response['_ms'])}ms | {query[:70]}")
        print(name, metrics(ranks))


def cmd_fetch(args: argparse.Namespace) -> None:
    jobs: list[tuple[str, str | None, str, str]] = [(f"set:{name}", expect_id, query, PROJECT) for name, rows in load_sets().items() for _, expect_id, query in rows]
    for item in json.loads(GOLDEN.read_text(encoding="utf-8"))["items"]:
        if item.get("expect_any") and not item.get("expect_route") and item.get("project"):
            jobs.append((f"golden:{item['id']}", None, item["q"], item["project"]))
    pool = []
    for index, (kind, expect_id, query, project) in enumerate(jobs):
        response = search(query, project=project, limit=100)
        pool.append({"kind": kind, "exp": expect_id, "q": query, "hits": response["memories"]})
        if index % 10 == 0:
            print(index, len(jobs), flush=True)
    Path(args.out).write_text(json.dumps(pool, ensure_ascii=False), encoding="utf-8")


def cmd_replay(args: argparse.Namespace) -> None:
    from blackholememory.app_core_hit import _hybrid_hit_text
    from blackholememory.retrieval_diversity import mmr_select
    from blackholememory.retrieval_rank_tuning import checkpoint_rank_multiplier

    pool = json.loads(Path(args.pool).read_text(encoding="utf-8"))
    golden = {i["id"]: i for i in json.loads(GOLDEN.read_text(encoding="utf-8"))["items"]}
    big = 10**6

    def order(hits: list[dict], protect: int, live_protect: int) -> list[dict]:
        os.environ["BHM_CHECKPOINT_PROTECT_RANK"] = str(live_protect)
        base = []
        for index, hit in enumerate(hits):
            md = hit.get("metadata") or {}
            old = float(md.get("checkpoint_rank_multiplier", 1.0)) or 1.0
            base.append((index, hit, float(md.get("rrf_score") or 0) / old, md.get("semantic_rank"), md.get("lexical_rank")))
        os.environ["BHM_CHECKPOINT_PROTECT_RANK"] = str(protect)
        ranked = sorted(
            ((i, h, score * checkpoint_rank_multiplier(h, semantic_rank=sr, lexical_rank=lr), sr, lr) for i, h, score, sr, lr in base),
            key=lambda e: (-e[2], e[3] if e[3] is not None else big, e[4] if e[4] is not None else big, -float(e[1].get("score") or 0), e[0]),
        )
        selected = mmr_select([_hybrid_hit_text(e[1]) for e in ranked], [e[2] for e in ranked], lambda_param=0.94)
        return [ranked[s.index][1] for s in selected]

    def rank_of(ordered: list[dict], record: dict) -> int | None:
        if record["exp"]:
            return next((k for k, h in enumerate(ordered, 1) if h.get("id") == record["exp"]), None)
        candidates = [c.lower() for c in golden[record["kind"].split(":")[1]]["expect_any"]]
        for k, h in enumerate(ordered, 1):
            blob = " ".join(str(h.get(f) or "") for f in ("id", "title", "content")).lower()
            if any(c in blob for c in candidates):
                return k
        return None

    groups: dict[str, list[dict]] = {}
    for record in pool:
        if "hits" not in record or (record["kind"].startswith("golden:") and not golden.get(record["kind"][7:], {}).get("expect_any")):
            continue
        groups.setdefault(record["kind"] if record["kind"].startswith("set:") else record["kind"].split(":")[0], []).append(record)
    baseline: dict[str, list[int | None]] = {}
    for protect in [int(v) for v in args.variants.split(",")]:
        for group, records in groups.items():
            ranks = [rank_of(order(r["hits"], protect, args.live_protect), r) for r in records]
            baseline.setdefault(group, ranks) if protect == 0 else None
            worse = sum(1 for a, b in zip(baseline.get(group, ranks), ranks) if (a or 99) < (b or 99))
            print(f"PROTECT={protect} {group} {metrics(ranks)} worse_than_0={worse}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("live").set_defaults(func=cmd_live)
    fetch = sub.add_parser("fetch")
    fetch.add_argument("--out", required=True)
    fetch.set_defaults(func=cmd_fetch)
    replay = sub.add_parser("replay")
    replay.add_argument("--pool", required=True)
    replay.add_argument("--variants", default="0,1,3,5,10")
    replay.add_argument("--live-protect", type=int, default=3, help="PROTECT_RANK of the service when the pool was fetched")
    replay.set_defaults(func=cmd_replay)
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
