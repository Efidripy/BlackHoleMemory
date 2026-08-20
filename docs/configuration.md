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
