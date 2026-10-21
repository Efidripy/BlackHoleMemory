# ruff: noqa

from __future__ import annotations

from ._common import *  # noqa: F401,F403
from ._common import _context_title, _normalize_search_payload, _repo_root
from .swarm_tools import _normalize_sandbox_result

class ChronicleLogger:
    def __init__(self, task_id: str):
        safe_task_id = "".join(char if char.isalnum() or char in "._-" else "_" for char in task_id).strip("_")
        if not safe_task_id:
            safe_task_id = "unnamed-task"
        self.task_id = safe_task_id
        self.base_dir = _repo_root() / ".runtime" / "logs" / "agents" / safe_task_id
        self.chronicle_path = self.base_dir / "chronicle.md"
        assert_safe_path(self.base_dir, reject_hardlink_target=False)
        replace_bytes_safely(
            self.chronicle_path,
            (
                f"# Developer Agent Chronicle\n\n- task_id: `{safe_task_id}`\n- created_at: `{_now_iso()}`\n\n"
            ).encode("utf-8"),
        )
        self._append_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"chronicle-{safe_task_id}")

    def log_heartbeat(self, message: str):
        print(f"[STEP: {message}]", file=sys.stdout, flush=True)

    def log_phase(self, phase: str, content: str) -> None:
        entry = f"## [PHASE: {phase}]\n\n- timestamp: `{_now_iso()}`\n\n{content.strip()}\n\n"
        future = self._append_executor.submit(self._append_text, entry)
        future.result(timeout=RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS)

    def log_retrieval(self, context: list[dict[str, Any]], mode: str, payload_summary: str) -> None:
        lines = [f"- mode: `{mode}`", f"- payload: {_limit_text(payload_summary, 700)}", f"- retrieved: {len(context)}"]
        if context:
            lines.append("")
            lines.append("| id | title | semantic_type | lifecycle | score |")
            lines.append("| --- | --- | --- | --- | --- |")
            for item in context[:10]:
                metadata = dict(item.get("metadata") or {})
                lines.append(
                    "| "
                    f"`{_limit_text(item.get('id'), 80)}` | "
                    f"{_limit_text(_context_title(item), 120)} | "
                    f"`{_limit_text(metadata.get('semantic_type'), 40)}` | "
                    f"`{_limit_text(metadata.get('lifecycle'), 40)}` | "
                    f"`{item.get('score', 0.0)}` |"
                )
        self.log_phase("RETRIEVAL", "\n".join(lines))

    def log_execution(self, iteration: int, solution_text: str, candidate_code: str) -> None:
        content = (
            f"- iteration: {iteration}\n"
            f"- generated_chars: {len(str(solution_text or ''))}\n"
            f"- code_chars: {len(str(candidate_code or ''))}\n\n"
            "```python\n"
            f"{_limit_text(candidate_code or solution_text, 5000)}\n"
            "```"
        )
        self.log_phase("EXECUTION", content)

    def log_verification(self, iteration: int, result: dict[str, Any], failure_summary: str) -> None:
        normalized = _normalize_sandbox_result(result)
        content = (
            f"- iteration: {iteration}\n"
            f"- success: `{normalized['success']}`\n"
            f"- exit_code: `{normalized['exit_code']}`\n"
            f"- summary: {_limit_text(failure_summary, 700)}\n\n"
            "### stdout\n\n"
            "```text\n"
            f"{_limit_text(normalized['stdout'], 2000)}\n"
            "```\n\n"
            "### stderr\n\n"
            "```text\n"
            f"{_limit_text(normalized['stderr'], 3000)}\n"
            "```"
        )
        self.log_phase("VERIFICATION", content)

    def log_final(self, status: str, summary: str) -> None:
        phase = "SUCCESS" if status == "SUCCESS" else "SUSPENSION"
        self.log_phase(phase, f"- status: `{status}`\n- summary: {_limit_text(summary, 1200)}")

    def log_iteration(self, iteration: int, thought: str, action: str, observation: str):
        safe_observation = _truncate_multiline(observation, 300)
        entry = (
            "## [PHASE: EXECUTION]\n\n"
            f"- timestamp: {_now_iso()}\n"
            f"- iteration: {iteration}\n"
            f"- thought: {_limit_text(thought, 1200)}\n"
            f"- action: {_limit_text(action, 1200)}\n\n"
            "### Observation\n\n"
            f"{safe_observation}\n\n"
        )
        future = self._append_executor.submit(self._append_text, entry)
        future.result(timeout=RUNTIME_CHRONICLE_APPEND_TIMEOUT_SECONDS)

    def save_json_stats(self, duration: float, iterations: int, status: str, tokens: dict):
        summary = (
            f"duration_seconds={round(float(duration), 3)}; iterations={int(iterations)}; "
            f"tokens_prompt={int(tokens.get('prompt') or 0)}; "
            f"tokens_completion={int(tokens.get('completion') or 0)}; "
            f"tokens_total={int(tokens.get('total') or 0)}"
        )
        self.log_final(status, summary)
        self._append_executor.shutdown(wait=True)

    def _append_text(self, content: str):
        append_bytes_safely(self.chronicle_path, content.encode("utf-8"))


@dataclass(frozen=True)
class BHMSearchResult:
    id: str
    content: str
    project: str
    metadata: dict[str, Any]
    score: float = 0.0


class BHMRestClient:
    def __init__(self, base_url: str, timeout: float):
        normalized = base_url.rstrip("/")
        if not is_local_endpoint(normalized):
            raise ValueError("BHM internal endpoint must be loopback/private")
        self.base_url = normalized
        self.timeout = _bounded_agent_http_timeout(timeout, upper_bound=BHM_INTERNAL_HTTP_TIMEOUT_SECONDS)

    def post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import sys

        owner = sys.modules.get("blackholememory.agents.developer_agent")
        token_provider = getattr(owner, "configured_caller_token", configured_caller_token)
        caller_token = token_provider()
        if not caller_token:
            raise RuntimeError("BHM caller credential is unavailable")
        with httpx.Client(
            base_url=self.base_url,
            timeout=self.timeout,
            headers={"Authorization": f"Bearer {caller_token}"},
            trust_env=False,
            follow_redirects=False,
        ) as client:
            response = client.post(path, json=payload)
            response.raise_for_status()
            return _bounded_httpx_json(response)

    def search(self, payload: dict[str, Any]) -> list[BHMSearchResult]:
        errors: list[str] = []
        profile = str(payload.get("retrieval_profile") or "fact_only")
        filters = dict(payload.get("filters") or {})
        if "quarantine-demo" in {
            str(payload.get("project") or "").lower(),
            str(payload.get("domain") or "").lower(),
            str(filters.get("project") or "").lower(),
            str(filters.get("domain") or "").lower(),
        }:
            raise RuntimeError("Direct semantic retrieval for quarantine-demo is forbidden; use QuarantineGatewayNode.")
        for path in ("/bhm/search", "/bhm/search/advanced"):
            try:
                data = self.post(path, payload)
                return _normalize_search_payload(
                    data,
                    allow_archived=bool(payload.get("include_archived")),
                    allow_raw=bool(payload.get("include_logs")),
                    profile=profile,
                )
            except (httpx.HTTPError, json.JSONDecodeError, TypeError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
                continue
        raise RuntimeError("; ".join(errors))

    def batch_upsert(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self.post("/bhm/memories/batch-upsert", {"items": items})

    def batch_link(self, items: list[dict[str, Any]]) -> dict[str, Any]:
        return self.post("/bhm/memories/batch-link", {"items": items})

    def synthesize_fact_crystal(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.post("/bhm/synthesis/fact-crystal", payload)


@dataclass(frozen=True)
class QuarantineGatewayResult:
    task_id: str
    generated_count: int
    retained_count: int
    chunk_count: int
    chunk_size: int
    filter_seconds: float
    fact_crystal: dict[str, Any]
    quarantine_file: str


class QuarantineGatewayNode:
    """
    Single ingress for destructive quarantine-demo stress work.

    Quarantine records are never sent to normal BHM retrieval. They live in
    .runtime/live-memory/quarantine-demo.json until teardown, while the optional
    external persistence path writes only a compact validated fact crystal.
    """

    def __init__(
        self,
        bhm_client: BHMRestClient | None = None,
        quarantine_file: Path | None = None,
        chunk_size: int = QUARANTINE_CHUNK_SIZE,
    ):
        self.bhm = bhm_client
        self.chunk_size = chunk_size
        self.quarantine_file = quarantine_file or (_repo_root() / ".runtime" / "live-memory" / QUARANTINE_DEMO_FILE)
        assert_safe_path(self.quarantine_file.parent, reject_hardlink_target=False)
        if not self.quarantine_file.exists():
            self._atomic_write_json([])

    def __enter__(self) -> "QuarantineGatewayNode":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.teardown()

    def execute(
        self,
        task_query: str = "quarantine-demo avalanche failure emulator",
        log_count: int = 100_000,
        task_id: str | None = None,
        auto_teardown: bool = True,
        persist_fact_crystal: bool = False,
    ) -> QuarantineGatewayResult:
        safe_task_id = task_id or f"{QUARANTINE_TASK_PREFIX}{_stable_key(task_query, str(log_count), _now_iso())}"
        if not safe_task_id.startswith(QUARANTINE_TASK_PREFIX):
            safe_task_id = f"{QUARANTINE_TASK_PREFIX}{safe_task_id}"
        logger = ChronicleLogger(safe_task_id)

        try:
            records = self._generate_avalanche_logs(task_query, log_count)
            started = time.perf_counter()
            retained = [
                record
                for record in records
                if record["semantic_type"] == "error" and record["quarantine_scope"] == "quarantine-demo"
            ]
            filter_seconds = time.perf_counter() - started
            retained_ids = [str(record["id"]) for record in retained]
            chunks = self.chunk_ids(retained_ids)
            self._atomic_write_json(retained)
            fact_crystal = self._build_fact_crystal(
                task_query=task_query,
                generated_count=len(records),
                retained_count=len(retained),
                chunk_count=len(chunks),
                filter_seconds=filter_seconds,
            )
            if persist_fact_crystal:
                if self.bhm is None:
                    raise RuntimeError("persist_fact_crystal=True requires a BHMRestClient")
                self.bhm.batch_upsert([fact_crystal])

            logger.log_phase(
                "RETRIEVAL",
                (
                    "- mode: `quarantine-gateway-only`\n"
                    "- source: `synthetic avalanche emulator`\n"
                    f"- generated_count: `{len(records)}`\n"
                    f"- retained_count: `{len(retained)}`\n"
                    f"- chunk_count: `{len(chunks)}`"
                ),
            )
            logger.log_phase(
                "VERIFICATION",
                (
                    f"- filter_seconds: `{round(filter_seconds, 6)}`\n"
                    f"- chunk_size: `{self.chunk_size}`\n"
                    f"- quarantine_file: `{self.quarantine_file}`"
                ),
            )
            result = QuarantineGatewayResult(
                task_id=safe_task_id,
                generated_count=len(records),
                retained_count=len(retained),
                chunk_count=len(chunks),
                chunk_size=self.chunk_size,
                filter_seconds=filter_seconds,
                fact_crystal=fact_crystal,
                quarantine_file=str(self.quarantine_file),
            )
            logger.save_json_stats(filter_seconds, 1, "SUCCESS", {"prompt": 0, "completion": 0, "total": 0})
            return result
        finally:
            if auto_teardown:
                self.teardown()

    def chunk_ids(self, ids: list[str]) -> list[list[str]]:
        return [ids[index : index + self.chunk_size] for index in range(0, len(ids), self.chunk_size)]

    def teardown(self) -> None:
        self._atomic_write_json([])
        logs_root = _repo_root() / ".runtime" / "logs" / "agents"
        for chronicle_path in logs_root.glob(f"{QUARANTINE_TASK_PREFIX}*/chronicle.md"):
            try:
                chronicle_path.unlink()
            except FileNotFoundError:
                continue

    def _atomic_write_json(self, payload: list[dict[str, Any]]) -> None:
        replace_bytes_safely(
            self.quarantine_file,
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        )

    def _generate_avalanche_logs(self, task_query: str, log_count: int) -> list[dict[str, Any]]:
        count = max(0, int(log_count))
        duplicate_digest = _stable_key(task_query, "cyclic-network-spam")
        return [
            {
                "id": f"quarantine-demo-{index:08d}",
                "project": "quarantine-demo",
                "kind": "observation",
                "semantic_type": "error",
                "quarantine_scope": "quarantine-demo",
                "content": f"cyclic network spam duplicate={duplicate_digest} index={index % 997}",
                "metadata": {
                    "lifecycle": "transient",
                    "semantic_type": "error",
                    "kind": "observation",
                    "duplicate_digest": duplicate_digest,
                },
            }
            for index in range(count)
        ]

    def _build_fact_crystal(
        self,
        task_query: str,
        generated_count: int,
        retained_count: int,
        chunk_count: int,
        filter_seconds: float,
    ) -> dict[str, Any]:
        digest = _stable_key(task_query, str(generated_count), str(retained_count), str(chunk_count))
        return {
            "upsert_key": f"quarantine-demo-fact-crystal:{digest}",
            "project": "blackholememory",
            "type": "fact-crystal",
            "content": (
                "quarantine-demo stress pass: "
                f"generated={generated_count}; retained={retained_count}; "
                f"archive_chunks={chunk_count}; chunk_size={self.chunk_size}; "
                f"filter_seconds={round(filter_seconds, 6)}"
            ),
            "concepts": ["quarantine-demo", "stress-test", "fact-crystal"],
            "metadata": {
                "lifecycle": "validated",
                "semantic_type": "fact",
                "quarantine_scope": "quarantine-demo",
                "generated_count": generated_count,
                "retained_count": retained_count,
                "archive_chunk_size": self.chunk_size,
                "archive_chunk_count": chunk_count,
                "filter_seconds": round(filter_seconds, 6),
                "version": "1.0",
            },
        }

__all__ = [name for name in globals() if not name.startswith("__")]
