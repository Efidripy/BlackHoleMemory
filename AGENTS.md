# BlackHoleMemory — проектные правила агента

Этот файл — проектный адаптер к workspace-канону. Обязательный источник общих
правил и startup-порядка: `E:\GitHub\AGENTS.md`.

## Workspace Starter

Перед нетривиальной задачей агент читает:

1. `E:\GitHub\START-HERE.md`;
2. `E:\GitHub\WORKSPACE.md`;
3. `E:\GitHub\workspace\WORKSPACE-OPERATING-SYSTEM.md`;
4. `E:\GitHub\AGENTS.md`;
5. этот файл и `README.md`.

## Границы BHM

- Каноническое локальное хранилище: `.runtime/live-memory/memories.sqlite3`.
- SQLite остаётся authoritative; Qdrant — восстанавливаемая projection.
- Тесты и smoke не должны писать в authoritative SQLite без отдельного
  явного контракта и backup/rollback receipt.
- `.runtime`, `.docs`, `.legacy` и `.src` — локальные/internal surfaces; в
  public tree попадают только разрешённые файлы согласно manifest.
- CAP12/CAP13/CAP14 — исторические retired labels, не acceptance и не backlog.

## Проверки

Для изменений запускать соразмерные lint, pytest, runtime smoke, public-tree
и acceptance gates. Секреты, токены и сырые runtime-логи не добавлять в Git.
