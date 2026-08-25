# Конфигурация

Проектные схемы и безопасные defaults находятся в `config/`. Локальные
переопределения задаются переменными окружения или отдельными файлами в
`.local/`; они не добавляются в Git.

Не коммитьте `.env`, токены, ключи, SQLite-файлы, runtime logs или release
archives. Для публичной конфигурации используйте только шаблоны и значения,
которые не зависят от конкретной машины.

Подробные внутренние операционные решения и acceptance receipts хранятся в
локальной `.docs/`-зоне разработчика.

## Core и Qdrant projection

`BHM_QDRANT_REQUIRED_FOR_CORE` управляет только связью readiness/startup с
Qdrant. Безопасный default — `true`: строгий профиль не стартует без projection.
Канонический Windows SQLite-authoritative launcher явно задаёт `false`, поэтому
SQLite API/MCP продолжают работать при сбое Docker/Qdrant, а detailed health и
SLO показывают деградацию. Переменная не включает embedded fallback и не меняет
SQLite authority boundary.

## Governed consolidation (default off)

`BHM_GOVERNED_CONSOLIDATION_ENABLED` defaults to unset/`false`. It enables only
the authenticated operator API/MCP surface for same-project proposal review;
it does not start a worker, poll an LLM, invoke Mem0, write Qdrant, migrate a
database, or apply a proposal by itself. The disabled status is the expected
production default until an operator schedules a controlled activation.

The additive proposal schema is deliberately **not** installed during startup.
Before any activation, stop SQLite writers, retain a distinct verified existing
backup, generate and bind a migration plan digest, apply the migration offline
with explicit confirmation, reopen and run readiness/parity smoke. The local
operator CLI requires both `--confirm` and `--offline-writer-verified` for the
migration path. Do not point it at a live database.

In the normal mode, every lifecycle mutation stays approval-gated: an operator
must approve one proposal and submit `apply=true` with the exact proposal ID as
confirmation. `BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED` is a third, separately
default-off opt-in for the semantic stored-proposal route only. When enabled,
BHM performs a deterministic policy review and uses the same exact-ID apply
path automatically, unless `BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED=1` is set.
That consent switch keeps proposals queued until the launcher operator presses
**Apply memory proposals**; the launcher previews the bounded queue, creates a
verified backup, then approves and applies only policy-eligible items. It
accepts only a confirmed local-model proposal with no conflicts and an
operation-specific high-confidence threshold (`0.90` for `create`/`revise`/`link`,
`0.97` for `supersede`/`archive`). It rejects no-op, fallback, incomplete
receipts, malformed or low-confidence candidates and records only the policy version, actor digest and
reason codes. Neither flag polls for work or generates a proposal from every
memory write.

Semantic degraded results are retried at most three bounded times. After a
fallback, no-op, conflict or low-confidence pass, lifecycle apply requires a
2-of-3 matching candidate digest; disagreement emits a no-op and performs no
authority mutation. `BHM_GOVERNED_SEMANTIC_REVIEW_MAX_ATTEMPTS` may lower the
bound (1–3) for a local test, but cannot raise it.

Proposal preview, inspection, validation and dry-run remain non-mutating.
Regardless of the mode, the apply revalidates revision/digest inside the SQLite
transaction and writes only the normal `memory_outbox`; it never writes Mem0 or
Qdrant directly. Disablement is reversible by removing the auto-review flag
and restarting the API; existing proposal evidence remains inspectable. The
authority contract is documented in [architecture-authority.md](architecture-authority.md).

### Local semantic proposal editor (default off)

`BHM_GOVERNED_SEMANTIC_EDITOR_ENABLED` defaults to unset/`false`. When it is
explicitly enabled alongside governed consolidation, the authenticated operator
surface may call a local-only OpenAI-compatible model to propose strict JSON.
It is not a worker or polling switch: one explicit request performs one bounded
foreground inference and returns a proposal or an error. Core API/MCP behavior
does not depend on it.

The canonical Windows launcher imports the explicit non-secret governed
configuration allowlist from Windows User environment into its child process.
This preserves an already approved local activation across a desktop restart;
it neither enables the feature by default nor imports caller credentials or
admin capability through that allowlist.

To enable the automatic policy stage after a disposable rehearsal, persist the
additional non-secret User setting and restart through the canonical launcher:

```powershell
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_AUTO_REVIEW_APPLY_ENABLED', '1', 'User')
[Environment]::SetEnvironmentVariable('BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED', '1', 'User')
```

Removing the auto-review value (or setting it to `0`) and restarting restores
the manual approval/apply mode. Keeping `BHM_GOVERNED_OPERATOR_CONSENT_REQUIRED=1`
selects the launcher-button consent mode. The REST status response exposes the active
policy version and thresholds without memory content.

Optional settings are `BHM_GOVERNED_SEMANTIC_EDITOR_BASE_URL`,
`BHM_GOVERNED_SEMANTIC_EDITOR_MODEL`,
`BHM_GOVERNED_SEMANTIC_EDITOR_TIMEOUT_SECONDS` (1–120; default `60`) and
`BHM_GOVERNED_SEMANTIC_EDITOR_MAX_TOKENS` (64–2048; default `180`). The compact
proposal budget is intentional: a larger prose-sized completion can exceed the
foreground deadline on the supported local 7B model despite a healthy provider.
The base
URL goes through the existing local-only endpoint policy, so a remote URL,
credentials in the URL, redirects and oversized response are rejected. See
[governed-semantic-editor.md](governed-semantic-editor.md) for the operator
sequence and shadow-mode evidence contract.

## Local loopback endpoints and IPv6

Каталог endpoint’ов принимает только сконфигурированные локальные hosts для
defaults BHM. IPv6 loopback опционален: указывайте bare host `::1` в переменной
окружения наподобие `BHM_LM_STUDIO_HOST`; BHM сериализует его в корректный URL
`http://[::1]:13666/v1`. Bracketed input (`[::1]`) также нормализуется для
совместимости. LAN, public и wildcard listener не являются заменой: local-only
проверка BHM остаётся fail-closed.

Это изменение лишь позволяет адресовать уже локально настроенный IPv6 listener.
Оно не запускает, не публикует и не перенастраивает LM Studio, llama-server,
BHM API, Qdrant или другой процесс.

Для фактической локальной проверки используйте
`uv run python scripts/validate-bhm-local-llm-dualstack.py`. Она делает только
два bounded `GET /v1/models` без proxy и redirects: `127.0.0.1` и `::1` на
одном порту. Статус `ipv4_only` означает, что BHM остаётся штатно доступен по
IPv4, а provider ещё не слушает IPv6; это не ошибка и не повод включать LAN
режим. `--require-ipv6` предназначен лишь для явного будущего dual-stack gate.

## SQLite history retention

Автоматическая bounded-очистка включена через
`BHM_SQLITE_RETENTION_ENABLED=true`. Безопасные defaults:

- initial delay: `BHM_SQLITE_RETENTION_INITIAL_DELAY_SECONDS=300`;
- interval: `BHM_SQLITE_RETENTION_INTERVAL_SECONDS=21600`;
- graph/index history: по `2` snapshot на scope;
- minimum age graph/index: `7` дней;
- completed outbox floor: `1000`, последний event каждого aggregate и возраст
  не менее `30` дней;
- один цикл: максимум `2` graph, `2` index и `250` outbox rows.

Точные имена остальных переменных совпадают с полями `sqlite_retention_*` в
`src/blackholememory/config.py` и получают стандартный prefix `BHM_`.
Автоматический режим не выполняет backup или `VACUUM`. Операторский flow и
rollback описаны в [SQLite retention](sqlite-retention.md).

## Durable LangGraph checkpoints

Developer-agent graphs are **ephemeral by default**. Durable resume is an
operator-controlled SQLite feature, not a consequence of merely installing
LangGraph. The saver keeps orchestration tuples in dedicated
`bhm_langgraph_checkpoint_*` tables in the existing authoritative SQLite file;
it never writes Mem0 or Qdrant.

Activation is fail-closed and needs all four variables in the process that
executes `BHMAgentExecutor.execute_loop`:

```text
BHM_LANGGRAPH_DURABLE_CHECKPOINT_ENABLED=true
BHM_LANGGRAPH_DURABLE_CHECKPOINT_ALLOW_AUTHORITATIVE=true
BHM_LANGGRAPH_DURABLE_CHECKPOINT_SCHEMA=bhm.langgraph.checkpoint.sqlite.v1
BHM_LANGGRAPH_DURABLE_CHECKPOINT_CALLER_ID=<stable-local-caller-id>
```

`BHM_LANGGRAPH_DURABLE_CHECKPOINT_SESSION_ID` is optional; if omitted, the
task ID supplies a stable resume scope. The caller ID is an identifier, never a
bearer token or secret. A missing acknowledgement, schema mismatch, or empty
caller ID rejects a requested resumable run before graph compilation and before
SQLite schema creation. Passing `resumable=False` to `execute_loop` forces a
one-shot ephemeral graph even during an activation window.

Before enabling the variables for a live runtime, create a verified SQLite
backup, run the crash/reopen/concurrency/prune drill, and keep the rollback as
removing the two enablement variables and restarting only the affected agent
process. Disabling the feature does not modify BHM memory lifecycle records or
Qdrant; existing checkpoint rows remain inert until explicitly pruned through a
separate reviewed operation.

## One-time data hygiene

`config/data-hygiene-policy.json` — точный одноразовый allowlist disposable
project IDs и отдельный denylist защищённых проектов. Сопоставление выполняется
только по полному project ID: wildcard, prefix, regex и эвристика по имени
запрещены. Политика требует существующий full SQLite backup, не создаёт второй
полный backup, не выполняет `VACUUM`, а новые operational artifacts ограничивает
каталогом `.runtime/data-hygiene/`.

Изменение списка — отдельная reviewed операция. Автоматическое добавление
будущих `*-test` или `*-smoke` проектов не допускается. Operator flow описан в
[Data hygiene](data-hygiene.md).
