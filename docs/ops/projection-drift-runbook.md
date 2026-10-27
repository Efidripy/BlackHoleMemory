# Runbook: BHM SLO BREACHED (projection route unavailable: binding_source_drift)

## Symptom
- Плитка дашборда **Projection P/F** отображает `0/N` (0 в очереди, N сбоев).
- Статус системы переходит в **BHM SLO BREACHED**.
- Проваливается SLO-проверка `projection_failed_within_budget` (допустимый бюджет: `0`), поскольку события `memory_outbox` переходят в терминальный статус `dead_letter` с ошибкой `projection route unavailable: binding_source_drift`.

## Cause
- Хеш источника поколения (`generation source digest`) изменяется (дрейфует), когда любой проект в группе поколения получает новые воспоминания.
- Ранее этот дрейф приводил к закрытию маршрута проекции. После исправления маршрут остаётся открытым со статусом `binding_active_source_drift`, а факт дрейфа остаётся только предупреждением (warning).

## Diagnosis
- Выполните read-only запрос к SQLite базе (строго с `mode=ro`, например `file:.../memories.sqlite3?mode=ro`):
  ```sql
  SELECT status, count(*) FROM memory_outbox GROUP BY 1;
  ```
- Убедитесь, что записи со статусом `dead_letter` содержат ошибку дрейфа:
  ```sql
  SELECT event_id, last_error FROM memory_outbox WHERE status = 'dead_letter';
  ```

## Fix
Сначала убедитесь, что запущена версия BHM с исправлением маршрута (`bhm_health` -> `source_identity.revision`): иначе возвращённые события снова упадут в `dead_letter`.

1. Создайте резервную копию базы через SQLite Online Backup API.
2. Запустите сухой прогон (dry-run) для предварительного просмотра и валидации дайджеста без записи:
   ```bash
   PYTHONPATH=src python scripts/requeue-bhm-dead-letters.py
   ```
3. Выполните фактический возврат событий в статус `pending`, передав каталог резервной копии:
   ```bash
   PYTHONPATH=src python scripts/requeue-bhm-dead-letters.py --apply --backup-root .runtime/backups/requeue
   ```
4. Проверьте статус восстановления и метрики SLO:
   ```powershell
   scripts/bhm-projection-operator.ps1 -Action status
   ```

## Never
- **Никогда не удаляйте строки `memory_outbox` вручную** (`DELETE` нарушает строгую последовательность и аудит).
- **Никаких секретов**: не выводите и не сохраняйте токены (`BHM_CALLER_TOKEN`) или API-ключи в терминале, тикетах и репозитории.
