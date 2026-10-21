# BlackHoleMemory v1.8.7 — security dependency update and safer Inject

## Release identity

- release version: `1.8.7`;
- channel: `PURE`;
- runtime: `bhm-v1.8.7-PURE`;
- broker: `ipc-broker-v1.8.7`;
- UI: `Runtime v1.8.7-PURE`;
- plugin: `1.8.7`.

## Included changes

- **Security: `certifi` 2026.10.19.** Closes the critical Dependabot alert for
  the compromised intermediate keys of the emSign root CAs (first patched
  version). The bundled launcher carries the updated CA bundle; restart the
  service so its virtual environment picks up the new lock.
- **Inject warns while Claude Desktop is running.** Claude Desktop rewrites
  `claude_desktop_config.json` from its in-memory copy and erases the entry the
  launcher wrote. The Inject action now asks (default No) to close Claude Desktop
  first, in both the onboarding screen and the Integrations window, and the success
  message reminds to restart Claude Desktop and Codex. The Claude Code binary
  bundled with the app (same `claude.exe` name) is not treated as the app.
- No server, API, MCP or data-model changes.

## Validation

- full pytest gate, Ruff, public-tree and docs-link checks (see the release commit);
- launcher-focused tests: 190 passed, including the new Inject-warning tests.
