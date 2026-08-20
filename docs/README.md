# Документация BlackHoleMemory

Здесь находится только документация пользователя — без ADR, внутренних планов,
операционных receipts и истории разработки.

- [Быстрый старт](getting-started.md) — установка, запуск и проверка readiness.
- [Использование](usage.md) — REST, MCP, Galaxy и базовые сценарии.
- [Конфигурация](configuration.md) — локальные настройки и границы данных.
- [Маршрутизация агентов и моделей](agent-model-routing.md) — lightweight-first
  policy, capability gates и границы эскалации.
- [SQLite retention](sqlite-retention.md) — автоматическая и офлайн-очистка истории.
- [Data hygiene](data-hygiene.md) — двухфазная очистка точных disposable project IDs.
- [Диагностика](troubleshooting.md) — что проверить, если runtime не стартует.
- [Benchmarks](benchmarks/bhm-value-benchmark.md) — публичная методика и ограничения измерений.

Внутренняя документация разработчиков хранится локально в `.docs/` и намеренно
не входит в public GitHub tree.
