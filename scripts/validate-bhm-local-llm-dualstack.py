"""Probe the local OpenAI-compatible LLM on IPv4 and IPv6 loopback only."""

from __future__ import annotations

import argparse
import json

from blackholememory.local_llm_dualstack import DEFAULT_TIMEOUT_SECONDS
from blackholememory.local_llm_dualstack import dualstack_report
from blackholememory.runtime_endpoints import endpoint_port


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=endpoint_port("lm_studio"))
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--require-ipv6", action="store_true")
    args = parser.parse_args()
    try:
        report = dualstack_report(args.port, timeout_seconds=args.timeout_seconds)
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": type(exc).__name__, "detail": str(exc)[:240]}, sort_keys=True))
        return 2
    report["ipv6_required"] = bool(args.require_ipv6)
    report["ok"] = bool(report["ok"] and (not args.require_ipv6 or report["readiness"] == "dual_stack_ready"))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report["ok"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
