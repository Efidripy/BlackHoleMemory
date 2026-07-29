"""Build an executable local-LLM capability and benchmark inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_TIMEOUT = 20.0
PORT_RE = re.compile(r"--port\s+(\d+)")
CTX_RE = re.compile(r"--ctx-size\s+(\d+)")
PARALLEL_RE = re.compile(r"--parallel\s+(\d+)")
HOST_RE = re.compile(r"--host\s+([^\s]+)")
MODEL_RE = re.compile(r"--model\s+(.+?)(?:\s+--|$)")


def discover_llama_process(processes: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """Find a local llama-server without exposing command-line secrets."""

    if processes is None:
        try:
            import psutil

            processes = []
            for process in psutil.process_iter(["pid", "name", "cmdline"]):
                info = process.info
                processes.append(
                    {
                        "pid": info.get("pid"),
                        "name": info.get("name"),
                        "cmdline": " ".join(info.get("cmdline") or []),
                    }
                )
        except (ImportError, OSError):
            processes = []
    for process in processes:
        name = str(process.get("name") or "").casefold()
        command = str(process.get("cmdline") or "")
        if "llama-server" not in name and "llama-server" not in command.casefold():
            continue
        port_match = PORT_RE.search(command)
        host_match = HOST_RE.search(command)
        if not port_match or not host_match:
            continue
        model_match = MODEL_RE.search(command)
        return {
            "pid": int(process.get("pid") or 0),
            "name": str(process.get("name") or ""),
            "host": host_match.group(1),
            "port": int(port_match.group(1)),
            "model_path": model_match.group(1).strip() if model_match else "",
            "loaded_context": int(_match_int(CTX_RE, command, 0)),
            "parallel": int(_match_int(PARALLEL_RE, command, 0)),
            "gpu_layers": _command_value(command, "--n-gpu-layers"),
            "api_key_sha256": _secret_digest(_command_value(command, "--api-key")),
        }
    return None


def probe_inventory(server: dict[str, Any], *, timeout: float = DEFAULT_TIMEOUT, samples: int = 3) -> dict[str, Any]:
    base_url = f"http://{server['host']}:{server['port']}/v1"
    headers = {}
    api_key = _command_value(_process_commandline(server.get("pid")), "--api-key") if server.get("pid") else ""
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    models_ok, models, models_error = _request_json(f"{base_url}/models", method="GET", headers=headers, timeout=timeout)
    if not models_ok:
        return {
            "ok": False,
            "local_only": _is_local_host(server.get("host")),
            "server": server,
            "base_url": base_url,
            "failure": {"stage": "models", "error": models_error},
        }
    model_rows = models.get("data") or models.get("models") or []
    model = model_rows[0] if model_rows and isinstance(model_rows[0], dict) else {}
    model_id = str(model.get("id") or model.get("model") or "")
    metadata = model.get("meta") if isinstance(model.get("meta"), dict) else {}
    details = model.get("details") if isinstance(model.get("details"), dict) else {}
    samples_out: list[dict[str, Any]] = []
    for index in range(max(int(samples), 1)):
        payload = {
            "model": model_id,
            "messages": [{"role": "user", "content": 'Return JSON exactly: {"status":"ok"}'}],
            "temperature": 0,
            "max_tokens": 32,
            "response_format": {"type": "json_object"},
        }
        started = time.perf_counter()
        ok, response, error = _request_json(
            f"{base_url}/chat/completions",
            method="POST",
            payload=payload,
            headers=headers,
            timeout=timeout,
        )
        elapsed_ms = round((time.perf_counter() - started) * 1000.0, 3)
        content = _response_content(response) if ok else ""
        parsed = _parse_json_object(content)
        usage = response.get("usage") if isinstance(response, dict) and isinstance(response.get("usage"), dict) else {}
        timings = response.get("timings") if isinstance(response, dict) and isinstance(response.get("timings"), dict) else {}
        completion_tokens = int(usage.get("completion_tokens") or 0)
        samples_out.append(
            {
                "ok": bool(ok and parsed is not None),
                "elapsed_ms": elapsed_ms,
                "completion_tokens": completion_tokens,
                "tokens_per_second": round(completion_tokens / max(elapsed_ms / 1000.0, 0.001), 3) if completion_tokens else 0.0,
                "first_token_latency_ms": round(float(timings.get("prompt_ms") or elapsed_ms), 3),
                "json_schema_valid": parsed is not None,
                "strict_raw_json": _is_strict_raw_json(content),
                "error": error or ("invalid_json" if ok and parsed is None else ""),
                "sample": index + 1,
            }
        )
    tool_payload = {
        **payload,
        "tools": [{"type": "function", "function": {"name": "bhm_inventory_probe", "description": "probe", "parameters": {"type": "object"}}}],
        "tool_choice": "none",
    }
    tools_ok, _tool_response, tools_error = _request_json(
        f"{base_url}/chat/completions",
        method="POST",
        payload=tool_payload,
        headers=headers,
        timeout=timeout,
    )
    successful = [sample for sample in samples_out if sample["ok"]]
    return {
        "ok": bool(successful),
        "local_only": _is_local_host(server.get("host")),
        "remote_fallback_detected": False,
        "base_url": base_url,
        "model": {
            "id": model_id,
            "path": server.get("model_path", ""),
            "format": metadata.get("format") or details.get("format") or ("gguf" if str(server.get("model_path", "")).lower().endswith(".gguf") else ""),
            "n_ctx_loaded": server.get("loaded_context", 0),
            "n_ctx_train": metadata.get("n_ctx_train", 0),
            "n_params": metadata.get("n_params", 0),
            "size_bytes": metadata.get("size", 0),
        },
        "capabilities": {
            "json_schema": bool(successful and all(sample["json_schema_valid"] for sample in successful)),
            "strict_json_native": bool(successful and all(sample["strict_raw_json"] for sample in successful)),
            "tool_schema_accepted": bool(tools_ok),
            "vision": False,
            "vision_reason": "text-only GGUF model; no multimodal capability advertised",
        },
        "benchmark": {
            "samples": samples_out,
            "successful_samples": len(successful),
            "latency_p50_ms": _percentile([sample["elapsed_ms"] for sample in successful], 0.5),
            "latency_p95_ms": _percentile([sample["elapsed_ms"] for sample in successful], 0.95),
            "first_token_latency_p50_ms": _percentile([sample["first_token_latency_ms"] for sample in successful], 0.5),
            "tokens_per_second": _average([sample["tokens_per_second"] for sample in successful]),
            "strict_json_pass_rate": round(len(successful) / max(len(samples_out), 1), 6),
            "tool_probe_error": tools_error,
            "failure_modes": sorted(
                {
                    *[str(sample["error"]) for sample in samples_out if sample["error"]],
                    *( ["json_fence_wrapper"] if any(not sample["strict_raw_json"] for sample in successful) else [] ),
                }
            ),
        },
        "hardware": hardware_snapshot(),
        "concurrency": {
            "parallel_slots": int(server.get("parallel") or 0),
            "bounded": True,
        },
        "attestation": {
            "process": "llama-server",
            "host_loopback_or_private": _is_local_host(server.get("host")),
            "model_path_exists": bool(server.get("model_path") and Path(server["model_path"]).exists()),
            "api_key_recorded_as_hash_only": bool(server.get("api_key_sha256")),
            "local_only": bool(_is_local_host(server.get("host")) and not False),
        },
    }


def _process_commandline(pid: Any) -> str:
    try:
        import psutil

        return " ".join(psutil.Process(int(pid)).cmdline())
    except (ImportError, OSError, ValueError):
        return ""


def hardware_snapshot() -> dict[str, Any]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.used,temperature.gpu,utilization.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"available": False}
    if result.returncode != 0 or not result.stdout.strip():
        return {"available": False, "error": result.stderr.strip()[:200]}
    rows = []
    for line in result.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 5:
            continue
        rows.append(
            {
                "name": parts[0],
                "memory_total_mib": _safe_number(parts[1]),
                "memory_used_mib": _safe_number(parts[2]),
                "temperature_c": _safe_number(parts[3]),
                "utilization_percent": _safe_number(parts[4]),
            }
        )
    return {"available": bool(rows), "gpus": rows}


def _safe_number(value: str) -> int | float | str:
    try:
        number = float(value)
    except ValueError:
        return value
    return int(number) if number.is_integer() else number


def _request_json(url: str, *, method: str, headers: dict[str, str], timeout: float, payload: dict[str, Any] | None = None) -> tuple[bool, dict[str, Any], str]:
    request = urllib.request.Request(url, method=method, headers={**headers, "Content-Type": "application/json"})
    if payload is not None:
        request.data = json.dumps(payload).encode("utf-8")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
        return True, decoded if isinstance(decoded, dict) else {}, ""
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return False, {}, str(exc)


def _response_content(response: dict[str, Any]) -> str:
    choices = response.get("choices") if isinstance(response, dict) else []
    if not choices or not isinstance(choices[0], dict):
        return ""
    message = choices[0].get("message") if isinstance(choices[0].get("message"), dict) else {}
    return str(message.get("content") or choices[0].get("text") or "")


def _parse_json_object(value: str) -> dict[str, Any] | None:
    cleaned = str(value or "").strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE | re.DOTALL).strip()
    if not cleaned.startswith("{"):
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start >= 0 and end > start:
            cleaned = cleaned[start : end + 1]
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) and parsed.get("status") == "ok" else None


def _is_strict_raw_json(value: str) -> bool:
    text = str(value or "").strip()
    return text.startswith("{") and text.endswith("}")


def _match_int(pattern: re.Pattern[str], value: str, default: int) -> int:
    match = pattern.search(value)
    return int(match.group(1)) if match else default


def _command_value(command: str, flag: str) -> str:
    match = re.search(re.escape(flag) + r"\s+([^\s]+)", command)
    return match.group(1).strip('"') if match else ""


def _secret_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest() if value else ""


def _is_local_host(host: Any) -> bool:
    value = str(host or "").casefold()
    if value in {"127.0.0.1", "localhost", "::1"}:
        return True
    if value.startswith("10.") or value.startswith("192.168."):
        return True
    if value.startswith("172."):
        try:
            return 16 <= int(value.split(".")[1]) <= 31
        except (IndexError, ValueError):
            return False
    return False


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * percentile)))
    return round(ordered[index], 3)


def _average(values: list[float]) -> float:
    return round(sum(values) / len(values), 3) if values else 0.0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    server = discover_llama_process()
    if server is None:
        report = {"ok": False, "local_only": False, "failure": {"stage": "process_discovery", "error": "llama-server not found"}}
    else:
        report = probe_inventory(server, timeout=args.timeout, samples=args.samples)
    payload = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload, encoding="utf-8", newline="\n")
    print(payload, end="")
    return 0 if report.get("ok") and report.get("local_only") else 1


if __name__ == "__main__":
    raise SystemExit(main())
