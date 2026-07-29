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

## Принцип безопасности

Операции изменения кода и данных proposal-only по умолчанию. Деструктивные
операции требуют явного действия оператора; локальные credentials, базы,
runtime logs и raw payload не являются частью public repository.
