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
