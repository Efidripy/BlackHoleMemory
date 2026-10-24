"""Report Python module line counts, deltas against baseline, and size ratchets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def count_lines(path: Path) -> int:
    """Return the number of lines in a UTF-8 text file."""
    with path.open("r", encoding="utf-8") as file:
        return sum(1 for _ in file)


def scan_modules(repo_root: Path) -> dict[str, int]:
    """Scan .py files under src/blackholememory and scripts, returning path: line_count."""
    targets = [
        repo_root / "src" / "blackholememory",
        repo_root / "scripts",
    ]
    sizes: dict[str, int] = {}
    for target in targets:
        if not target.exists():
            continue
        for file_path in sorted(target.rglob("*.py")):
            if file_path.is_file():
                rel_path = file_path.relative_to(repo_root).as_posix()
                sizes[rel_path] = count_lines(file_path)
    return sizes


def render_markdown_table(
    headers: list[str],
    alignments: list[str],
    rows: list[list[str]],
) -> str:
    """Format headers and rows into an aligned Markdown table."""
    col_widths = [len(h) for h in headers]
    for row in rows:
        for i, val in enumerate(row):
            col_widths[i] = max(col_widths[i], len(val))

    header_cells = [headers[i].ljust(col_widths[i]) for i in range(len(headers))]
    header_line = "| " + " | ".join(header_cells) + " |"

    sep_cells: list[str] = []
    for i, align in enumerate(alignments):
        width = max(col_widths[i], 3)
        if align == ":---:":
            sep_cells.append(":" + "-" * (width - 2) + ":")
        elif align == "---:":
            sep_cells.append("-" * (width - 1) + ":")
        else:
            sep_cells.append(":" + "-" * (width - 1))
    sep_line = "| " + " | ".join(sep_cells) + " |"

    row_lines: list[str] = []
    for row in rows:
        cells: list[str] = []
        for i, val in enumerate(row):
            align = alignments[i]
            width = col_widths[i]
            if align == "---:":
                cells.append(val.rjust(width))
            elif align == ":---:":
                cells.append(val.center(width))
            else:
                cells.append(val.ljust(width))
        row_lines.append("| " + " | ".join(cells) + " |")

    return "\n".join([header_line, sep_line] + row_lines) + "\n"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="Report line counts of Python modules under src/blackholememory and scripts.",
    )
    parser.add_argument("--top", type=int, default=30, help="Number of top files to display (default: 30).")
    parser.add_argument("--threshold", type=int, default=1500, help="Line count threshold for marking files (default: 1500).")
    parser.add_argument("--save-baseline", type=Path, metavar="PATH", help="Path to save baseline JSON {relative_path: line_count}.")
    parser.add_argument("--baseline", type=Path, metavar="PATH", help="Path to baseline JSON file for delta comparison.")
    parser.add_argument("--ratchet", action="store_true", help="Exit with code 1 if any baseline file grew or new file exceeds threshold.")
    parser.add_argument("--out", type=Path, metavar="PATH", help="Path to write Markdown output (defaults to stdout).")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Main execution entrypoint."""
    args = parse_args(argv)
    repo_root = Path(__file__).resolve().parents[1]

    current_sizes = scan_modules(repo_root)

    if args.save_baseline:
        save_path = args.save_baseline
        save_path.parent.mkdir(parents=True, exist_ok=True)
        with save_path.open("w", encoding="utf-8") as f:
            json.dump(current_sizes, f, indent=2, sort_keys=True)
            f.write("\n")

    baseline_data: dict[str, int] | None = None
    if args.baseline:
        if not args.baseline.is_file():
            print(f"error: baseline file not found: {args.baseline}", file=sys.stderr)
            return 1
        try:
            with args.baseline.open("r", encoding="utf-8") as f:
                raw_baseline = json.load(f)
            if not isinstance(raw_baseline, dict):
                print("error: baseline JSON must be an object", file=sys.stderr)
                return 1
            baseline_data = {
                str(k).replace("\\", "/"): int(v) for k, v in raw_baseline.items()
            }
        except Exception as exc:
            print(f"error loading baseline: {exc}", file=sys.stderr)
            return 1

    entries: list[dict[str, object]] = []
    if baseline_data is None:
        headers = ["File", "Lines", "Marker"]
        alignments = [":---", "---:", ":---:"]
        for path, count in current_sizes.items():
            marker = "[!]" if count > args.threshold else ""
            entries.append({"path": path, "lines": count, "marker": marker})
    else:
        headers = ["File", "Lines", "Delta", "Marker"]
        alignments = [":---", "---:", "---:", ":---:"]
        all_paths = set(current_sizes.keys()) | set(baseline_data.keys())
        for path in all_paths:
            if path in current_sizes and path in baseline_data:
                count = current_sizes[path]
                base_count = baseline_data[path]
                diff = count - base_count
                delta = f"+{diff}" if diff > 0 else (str(diff) if diff < 0 else "0")
                marker = "[!]" if count > args.threshold else ""
                entries.append({
                    "path": path,
                    "lines": count,
                    "delta": delta,
                    "marker": marker,
                })
            elif path in current_sizes:
                count = current_sizes[path]
                marker = "[!]" if count > args.threshold else ""
                entries.append({
                    "path": path,
                    "lines": count,
                    "delta": "NEW",
                    "marker": marker,
                })
            else:
                entries.append({
                    "path": path,
                    "lines": 0,
                    "delta": "GONE",
                    "marker": "",
                })

    entries.sort(key=lambda item: (-int(item["lines"]), str(item["path"])))

    if args.top >= 0:
        displayed = entries[: args.top]
    else:
        displayed = entries

    if baseline_data is None:
        table_rows = [[str(it["path"]), str(it["lines"]), str(it["marker"])] for it in displayed]
    else:
        table_rows = [
            [str(it["path"]), str(it["lines"]), str(it["delta"]), str(it["marker"])]
            for it in displayed
        ]

    markdown = render_markdown_table(headers, alignments, table_rows)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(markdown, encoding="utf-8")
    else:
        sys.stdout.write(markdown)

    if args.ratchet:
        violations: list[str] = []
        for path, count in current_sizes.items():
            if baseline_data is not None and path in baseline_data:
                base_count = baseline_data[path]
                if count > base_count:
                    violations.append(
                        f"{path} grew from {base_count} to {count} lines (+{count - base_count})"
                    )
            else:
                if count > args.threshold:
                    violations.append(
                        f"{path} exceeds threshold ({count} > {args.threshold} lines)"
                    )
        if violations:
            print(
                f"error: ratchet failure: {len(violations)} violation(s)",
                file=sys.stderr,
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
