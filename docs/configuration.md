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
