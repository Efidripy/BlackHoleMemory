# Диагностика

1. Проверьте readiness:

   ```powershell
   Invoke-WebRequest http://127.0.0.1:8000/health/ready
   ```

2. Если endpoint недоступен, запустите authoritative launcher из
   `scripts/start-bhm-authoritative.ps1` и проверьте Docker Desktop/Qdrant.
3. Если MCP не подключается, проверьте адрес `http://127.0.0.1:8000/mcp` и
   имя сервера `bhm`.
4. Не переносите в Git диагностические логи и raw receipts. Их место — в
   локальной `.local/`-зоне.

Если проблема воспроизводится после чистой установки, приложите версию,
команду запуска и обезличенный ответ readiness; секреты и полные payload не
прикладывайте.
