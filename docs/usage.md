# Использование

BlackHoleMemory — локальная память для AI-агентов. SQLite WAL хранит
authoritative lifecycle и metadata, Mem0 отвечает за semantic/logical layer,
Qdrant используется как восстанавливаемая vector projection, а LangGraph — для
оркестрации stateful flows.

## Authority and projection

SQLite is the authoritative store. In the canonical authoritative runtime the
projection worker is intentionally disabled by the SQLite authority guard;
this is a protection boundary, not a runtime failure. Qdrant remains a
rebuildable read projection and must not become a second source of truth.
The compatibility-sidecar boundary is documented in
[`docs/architecture-authority.md`](architecture-authority.md).

When an operator needs to reconcile a backlog, use the bounded operator flow:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\bhm-projection-operator.ps1 -Action drain -MaxCycles 32
```

The flow temporarily runs the explicit projection worker in `sqlite-shadow`
mode, then restores the canonical `sqlite-authoritative` runtime and verifies
that pending and failed projection events are zero.

The browser UI is launcher-session-bound: start the trusted BHM launcher, which
uses the caller bearer boundary and passes a one-time bootstrap token in the
URL fragment. Bare anonymous browser requests to `/bhm/ui/session/bootstrap`
are rejected even on loopback; do not expose `/` or `/bhm/*` through an
untrusted reverse proxy, and do not treat `X-Forwarded-*` headers as an
authentication signal. A proxy deployment must preserve the loopback boundary
and explicitly authenticate before forwarding requests.

## MCP

Канонический локальный endpoint:

```text
http://127.0.0.1:8000/mcp
```

Подключайте агента к серверу `bhm` через Streamable HTTP. Для проверки сначала
убедитесь, что `/health/ready` возвращает успешный ответ.

### Лимит и очередь сессий

Транспорт держит до 32 одновременно admitted MCP-сессий. Если лимит занят,
новые `initialize` ждут в FIFO-очереди и не создают SDK-сессию заранее. После
`DELETE`, idle expiry или transport loss освобождённый слот получает первый
ожидающий клиент. Состояние доступно в `/bhm/health` в полях
`active_count`, `max_sessions`, `queued_count` и `pending_count`.

Очередь ограничивает только MCP transport lifecycle; она не является очередью
записи памяти и не создаёт записи в BHM сама по себе.

REST/MCP clients should use the shared [BHM error taxonomy](error-taxonomy.md)
instead of parsing free-form error messages.

### Bounded repository indexing

`bhm_index_repository` выполняет не более 666 файлов за один MCP-вызов и
возвращает устойчивый `job_id`, progress и `index_next`. Если `status=running`,
повторите вызов с полями из `index_next`; forced refresh продолжается с
`force_refresh=false`, чтобы не создать новый epoch.
Continuation fields also include `expected_job_id` and `expected_state_digest`; pass them
unchanged so a repository mutation between slices fails closed instead of silently
starting a different job.

Построение code graph намеренно отделено от индексного slice. После
`status=completed` ответ содержит `graph_next` с точным `snapshot_id`.
Передайте этот receipt обратно в `bhm_index_repository`; `graph_only=true`
fail-closed отклонит незавершённый или устаревший snapshot. MCP использует
operation-specific deadlines: 60 секунд для bounded index, 90 секунд для graph
и 30 секунд для status; общий 15-секундный timeout остальных внутренних
вызовов не меняется.

## Принцип безопасности

Операции изменения кода и данных proposal-only по умолчанию. Деструктивные
операции требуют явного действия оператора; локальные credentials, базы,
runtime logs и raw payload не являются частью public repository.
