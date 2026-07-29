from __future__ import annotations

import argparse
from pathlib import Path

from blackholememory.value_benchmark import DEFAULT_CASE_COUNT
from blackholememory.value_benchmark import DEFAULT_REPEAT_COUNT
from blackholememory.value_benchmark import run_value_benchmark
from blackholememory.value_benchmark import write_value_benchmark_report


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the deterministic BHM Value Benchmark.")
    parser.add_argument("--cases", type=int, default=DEFAULT_CASE_COUNT)
    parser.add_argument("--repeats", type=int, default=DEFAULT_REPEAT_COUNT)
    parser.add_argument("--output-dir", type=Path, default=Path(".artifacts/benchmarks/bhm-value-v1"))
    args = parser.parse_args()

    report = run_value_benchmark(case_count=args.cases, repeats=args.repeats)
    output_json = args.output_dir / "report.json"
    output_markdown = args.output_dir / "summary.md"
    write_value_benchmark_report(report, output_json, output_markdown)
    print(f"benchmark={report['benchmark']} cases={report['case_count']} repeats={report['repeat_count']}")
    print(f"fixture_digest={report['fixture_digest']}")
    for mode, aggregate in report["aggregates"].items():
        print(
            f"{mode}: task_success={aggregate['task_success_rate']:.3f} "
            f"recall_at_5={aggregate['recall_at_5']:.3f} "
            f"citation_validity={aggregate['citation_validity']:.3f} "
            f"leakage={aggregate['leakage_count']:.0f} "
            f"tokens={aggregate['context_tokens_mean']:.1f}"
        )
    print(f"json={output_json}")
    print(f"markdown={output_markdown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
