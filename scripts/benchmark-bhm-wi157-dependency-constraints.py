"""Offline benchmark for the bounded WI-157 dependency-constraint receipt."""

from __future__ import annotations

import hashlib
import json
import tempfile
import time
from pathlib import Path

from blackholememory.package_resolution import resolve_package_manifests
from blackholememory.package_resolution_receipt import build_package_resolution_receipt


ITERATIONS = 1000
FIXTURE = {
    "dependencies": {
        "exact": "1.2.3",
        "range": ">=2,<3",
        "wild": "*",
        "workspace": "workspace:*",
        "local": "file:../local",
        "remote": "https://example.invalid/pkg.tgz",
    },
    "devDependencies": {"pytest": "^8"},
}


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="bhm-wi157-") as directory:
        root = Path(directory)
        payload = json.dumps(FIXTURE, sort_keys=True, separators=(",", ":")).encode("utf-8")
        (root / "package.json").write_bytes(payload)
        fixture_digest = hashlib.sha256(payload).hexdigest()
        durations: list[float] = []
        first: dict[str, object] | None = None
        for _ in range(ITERATIONS):
            started = time.perf_counter_ns()
            result = resolve_package_manifests(root)
            receipt = build_package_resolution_receipt(result)
            durations.append((time.perf_counter_ns() - started) / 1_000_000)
            if first is None:
                first = receipt
        ordered = sorted(durations)
        p50 = ordered[len(ordered) // 2]
        p95 = ordered[int(len(ordered) * 0.95) - 1]
        print(
            json.dumps(
                {
                    "schema_version": "bhm.p28.wi157.dependency-constraint-benchmark.v1",
                    "extractor": "bhm.package-resolution.v1",
                    "iterations": ITERATIONS,
                    "deterministic": bool(first and first["evidence_digest"] == receipt["evidence_digest"]),
                    "p50_ms": round(p50, 6),
                    "p95_ms": round(p95, 6),
                    "max_ms": round(max(durations), 6),
                    "fixture_digest": fixture_digest,
                    "constraint_kind_counts": (first or {}).get("summary", {}).get("constraint_kind_counts", {}),
                    "execution": {
                        "network": False,
                        "package_manager": False,
                        "compiler_or_lsp": False,
                        "install": False,
                        "edges_promoted": False,
                        "store_writes": False,
                    },
                },
                sort_keys=True,
            )
        )


if __name__ == "__main__":
    main()
