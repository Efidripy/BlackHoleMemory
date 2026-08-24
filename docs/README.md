# Документация BlackHoleMemory

Здесь находится только документация пользователя — без ADR, внутренних планов,
операционных receipts и истории разработки.

- [Быстрый старт](getting-started.md) — установка, запуск и проверка readiness.
- [Использование](usage.md) — REST, MCP, Galaxy и базовые сценарии.
- [Конфигурация](configuration.md) — локальные настройки и границы данных.
- [Маршрутизация агентов и моделей](agent-model-routing.md) — lightweight-first
  policy, capability gates и границы эскалации.
- [SQLite retention](sqlite-retention.md) — автоматическая и офлайн-очистка истории.
- [Authority boundaries](architecture-authority.md) — SQLite/outbox authority и governed consolidation proposals.
- [Data hygiene](data-hygiene.md) — двухфазная очистка точных disposable project IDs.
- [Local artifact cleanup](local-artifact-cleanup.md) — fail-closed очистка только воспроизводимых локальных cache/scratch-артефактов.
- [Runtime artifact governance](runtime-artifact-governance.md) — ownership, protection и архивирование тяжёлых локальных receipts/rollback-артефактов.
- [Script surface policy](script-surface-policy.md) — public entrypoints, local-only tooling и канон имён.
- [Диагностика](troubleshooting.md) — что проверить, если runtime не стартует.
- [Benchmarks](benchmarks/bhm-value-benchmark.md) — публичная методика и ограничения измерений.

Внутренняя документация разработчиков хранится локально в `.docs/` и намеренно
не входит в public GitHub tree.
