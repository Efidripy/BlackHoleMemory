#!/usr/bin/env python
"""Measure the warm MCP initialize + tools/list attach contract."""

from __future__ import annotations

# The script adds the repository's src directory before importing project modules.
# ruff: noqa: E402

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from blackholememory.mcp_latency import MCP_ATTACH_MAX_MS
from blackholememory.mcp_latency import MCP_CATALOG_MAX_BYTES
from blackholememory.mcp_latency import evaluate_mcp_latency
from blackholememory.mcp_protocol_contract import CURRENT_PROTOCOL_VERSION


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=5, help="Warm attach samples to collect")
    parser.add_argument("--max-attach-ms", type=float, default=MCP_ATTACH_MAX_MS)
    parser.add_argument("--max-catalog-bytes", type=int, default=MCP_CATALOG_MAX_BYTES)
    return parser.parse_args()


async def _measure(iterations: int) -> tuple[list[float], int, dict[str, Any]]:
    os.environ.setdefault("BHM_MCP_SURFACE", "core")
    from blackholememory import app as bhm_app

    async def request(method: str, request_id: int) -> dict[str, Any]:
        params = {"protocolVersion": CURRENT_PROTOCOL_VERSION} if method == "initialize" else {}
        response = await bhm_app._handle_mcp_gateway_jsonrpc_async(
            {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
        )
        if not isinstance(response, dict):
            raise RuntimeError(f"MCP {method} returned no response")
        if "error" in response:
            raise RuntimeError(f"MCP {method} failed: {response['error']}")
        return response

    # Exclude import/FastMCP construction from the warm contract.
    await request("initialize", 0)
    warm_catalog = await request("tools/list", 1)
    samples_ms: list[float] = []
    catalog_bytes = len(json.dumps(warm_catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    for request_id in range(2, 2 + max(iterations, 1)):
        started = time.perf_counter()
        await request("initialize", request_id * 2)
        catalog = await request("tools/list", request_id * 2 + 1)
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        samples_ms.append(elapsed_ms)
        catalog_bytes = max(
            catalog_bytes,
            len(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")),
        )
    return samples_ms, catalog_bytes, warm_catalog["result"]


def main() -> int:
    args = parse_args()
    samples_ms, catalog_bytes, catalog_result = asyncio.run(_measure(args.iterations))
    report = evaluate_mcp_latency(
        samples_ms,
        catalog_bytes,
        max_attach_ms=args.max_attach_ms,
        max_catalog_bytes=args.max_catalog_bytes,
    )
    report["surface"] = "core"
    report["catalog_tools"] = len(catalog_result.get("tools", []))
    report["iterations_requested"] = args.iterations
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
