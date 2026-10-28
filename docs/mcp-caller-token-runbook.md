# BHM MCP caller-token runbook

Этот runbook описывает безопасное подключение локального MCP-клиента к BHM.
Он предназначен для авторизованной loopback-среды; production deployment и
публикация credential в репозитории не входят в процедуру.

## Контракт

- Endpoint: `http://127.0.0.1:8000/mcp`.
- Transport: Streamable HTTP.
- Каноническая операторская HTTP-регистрация: `Authorization: Bearer <BHM_ADMIN_CAPABILITY>`.
- Для доступа вызывающего без операторской записи допустим `BHM_CALLER_TOKEN`.
- Источник истины для регистрации: `config/mcp-registration.json`.
- Claude Desktop использует описанный ниже stdio-мост; HTTP-контракт генератора
  адаптеров не заменяет этот мост.
- Claude Code: `~/.claude.json`, `mcpServers.bhm`; Codex: `~/.codex/config.toml`,
  `mcp_servers.bhm`, с `required = false`. Утверждённые headers helper
  отражают операторскую переменную; Claude helper допускает резервный caller token.

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

## Codex Desktop без process environment

Некоторые Windows Desktop/MSIX hosts не передают user-scoped environment в
дочерний Codex process. Симптом: BHM API и REST bridge healthy, но native MCP
не выдаёт tools, а `bearer_token_env_var = "BHM_CALLER_TOKEN"` обрывает
инициализацию до HTTP connect.

В этом случае используйте versioned plugin helper, который читает уже
существующий token из Windows user environment и печатает для Codex только
JSON HTTP headers. Token не записывается в `config.toml`:

```toml
[mcp_servers.bhm]
url = "http://127.0.0.1:8000/mcp"
http_headers_helper = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\\Users\\<user>\\.codex\\plugins\\local\\bhm-codex-connector\\scripts\\emit-bhm-mcp-headers.ps1"
```

Для helper-варианта удалите `bearer_token_env_var`: Codex требует эту
переменную до запуска helper. Сохраните backup user-level `config.toml`,
перезапустите только Codex Desktop и подтвердите attach native `bhm_health`.
BHM API перезапускать не требуется, если `/health/ready` уже возвращает `200`.

## Операторская запись (`bhm_remember`)

MCP-вызов `bhm_remember` пишет в авторитетную память только как оператор.
Сервер считает вызов `/mcp` операторским, если bearer равен операторской
капабилити `BHM_ADMIN_CAPABILITY` (либо она пришла в заголовке
`x-bhm-admin-capability`); обычный `BHM_CALLER_TOKEN` даёт только доступ
вызывающего, и запись отклоняется как `non_authoritative_ingress`. Капабилити
не принимается из `_meta` и аргументов инструмента.

- **Создание.** Скрипты запуска сервиса создают `BHM_ADMIN_CAPABILITY` в
  пользовательском окружении, если её нет (существующая не перезаписывается и
  не печатается). Ротация — `Rotate-BhmAdminCapability` из
  `scripts/bhm-caller-credential.ps1`; после неё нужно перезапустить BHM и
  клиентов.
- **Claude Desktop.** Кнопка Inject в окне Integrations лаунчера записывает
  `bhm` как stdio-мост `run-bhm-mcp-remote.ps1` из каталога плагина (секрет в
  конфиг не пишется). Claude Desktop передаёт серверу только `Authorization`,
  поэтому капабилити едет именно как bearer. Старая запись `url` +
  `bearer_token_env_var` записи не даёт.
- **Codex.** В `[mcp_servers.bhm]` оставьте `http_headers_helper` и укажите
  `bearer_token_env_var = "BHM_ADMIN_CAPABILITY"`. Проверено: Codex CLI с
  этими двумя настройками пишет память; конфигурация только с helper без
  `bearer_token_env_var` в проверке не подключила сервер. Для Desktop-хостов без
  user environment см. раздел выше и проверьте attach вручную: этот вариант в
  операторской записи не проверялся.
- **Диагностика.** Отказ `bhm_remember` содержит `operator_header`:
  `absent(bearer=caller)` — клиент прислал обычный токен (запись клиента
  откатилась, перезапустите Inject); `invalid` — прислано неверное значение.
  Значения секретов нигде не возвращаются.
- **Граница.** Капабилити — секрет оператора: не хранить в Git, `.docs`, BHM,
  логах и конфигах клиентов.

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
