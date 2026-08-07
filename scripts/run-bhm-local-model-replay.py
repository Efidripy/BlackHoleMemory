from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from blackholememory.local_model_replay import DEFAULT_BASE_URL
from blackholememory.local_model_replay import DEFAULT_MODEL_ID
from blackholememory.local_model_replay import DEFAULT_MODES
from blackholememory.local_model_replay import LOCAL_MODEL_REPLAY_DEFAULT_CASE_COUNT
from blackholememory.local_model_replay import LOCAL_MODEL_REPLAY_DEFAULT_REPEAT_COUNT
from blackholememory.local_model_replay import LOCAL_MODEL_REPLAY_EXPECTED_CALLS
from blackholememory.local_model_replay import run_local_model_replay
from blackholememory.local_model_replay import write_local_model_replay_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BHM local-model replay against frozen contexts.")
    parser.add_argument("--cases", type=int, default=LOCAL_MODEL_REPLAY_DEFAULT_CASE_COUNT)
    parser.add_argument("--repeats", type=int, default=LOCAL_MODEL_REPLAY_DEFAULT_REPEAT_COUNT)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL_ID)
    parser.add_argument("--modes", default=",".join(DEFAULT_MODES))
    parser.add_argument("--max-in-flight", type=int, default=2)
    parser.add_argument("--max-tokens", type=int, default=96)
    parser.add_argument("--tool-budget", type=int, default=0)
    parser.add_argument("--timeout-seconds", type=float, default=90.0)
    parser.add_argument("--output-dir", type=Path, default=Path(".runtime/local-model-replay-666"))
    args = parser.parse_args()
    modes = tuple(item.strip() for item in args.modes.split(",") if item.strip())
    total_model_calls = args.cases * args.repeats * len(modes)
    if total_model_calls != LOCAL_MODEL_REPLAY_EXPECTED_CALLS:
        parser.error(
            "local-model replay contract requires "
            f"exactly {LOCAL_MODEL_REPLAY_EXPECTED_CALLS} calls (cases × repeats × modes); "
            "the supplied workload is outside the bounded contract"
        )

    report = asyncio.run(
        run_local_model_replay(
            case_count=args.cases,
            repeats=args.repeats,
            base_url=args.base_url,
            model_id=args.model,
            modes=modes,
            max_in_flight=args.max_in_flight,
            max_tokens=args.max_tokens,
            tool_budget=args.tool_budget,
            timeout_seconds=args.timeout_seconds,
        )
    )
    output_json = args.output_dir / "report.json"
    output_markdown = args.output_dir / "summary.md"
    write_local_model_replay_report(report, output_json, output_markdown)
    print(f"benchmark={report['benchmark']} cases={report['case_count']} repeats={report['repeat_count']}")
    print(f"total_model_calls={report['total_model_calls']} call_budget={report['call_budget']['status']}")
    print(f"model={report['model']['model_id']} base_url={report['model']['base_url']}")
    print(f"fixture_digest={report['fixture_digest']}")
    for mode, aggregate in report["aggregates"].items():
        print(
            f"{mode}: task_success={aggregate['task_success_rate']:.3f} "
            f"json_validity={aggregate['json_validity']:.3f} "
            f"target_selected={aggregate['target_selected_rate']:.3f} "
            f"citation_validity={aggregate['citation_validity']:.3f} "
            f"failed_calls={aggregate['failed_calls']:.0f}"
        )
    print(f"json={output_json}")
    print(f"markdown={output_markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
