# Использование

BlackHoleMemory — локальная память для AI-агентов. SQLite WAL хранит
authoritative lifecycle и metadata, Mem0 отвечает за semantic/logical layer,
Qdrant используется как восстанавливаемая vector projection, а LangGraph — для
оркестрации stateful flows.

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

## Принцип безопасности

Операции изменения кода и данных proposal-only по умолчанию. Деструктивные
операции требуют явного действия оператора; локальные credentials, базы,
runtime logs и raw payload не являются частью public repository.
