# BHM MCP caller-token runbook

Этот runbook описывает безопасное подключение локального MCP-клиента к BHM.
Он предназначен для авторизованной loopback-среды; production deployment и
публикация credential в репозитории не входят в процедуру.

## Контракт

- Endpoint: `http://127.0.0.1:8000/mcp`.
- Transport: Streamable HTTP.
- Header: `Authorization: Bearer <BHM_CALLER_TOKEN>`.
- Источник истины для регистрации: `config/mcp-registration.json`.

Угловые скобки в примерах — placeholders. Их нельзя включать в фактический
token.

## Подготовка

1. Сгенерируйте caller token через локальный BHM operator flow. Не копируйте
   token в Git, shell history, issue или CI log.
2. Сохраните token в защищённом credential store хоста и передайте его
   клиенту через переменную окружения `BHM_CALLER_TOKEN` либо эквивалентный
   secret store клиента.
3. Зарегистрируйте MCP server `bhm` с endpoint из этого документа и bearer
   auth. Не добавляйте новый сервер, если уже существует canonical `bhm`.

## Проверка

- `401` означает отсутствующий, просроченный или неверно переданный token.
- Ошибка transport/connection означает проблему runtime или endpoint и не
  должна диагностироваться ротацией credential вслепую.
- После подключения проверьте native `bhm_health`: должны быть `healthy`,
  SQLite authoritative `ready`, MCP `attached`, а `contract_state` — `aligned`.
- Для текущей сессии отдельно подтвердите, что клиент видит ожидаемый набор
  инструментов; конфигурация файла сама по себе не доказывает attach.

## Ротация и восстановление

При компрометации немедленно отзовите token, создайте новый и повторите
проверку. Старый token удалите из credential store и локальных журналов.
Если runtime не готов, сначала восстановите `/health/ready` и только затем
повторяйте MCP attach. Runtime SQLite/Qdrant вручную не изменяйте.
