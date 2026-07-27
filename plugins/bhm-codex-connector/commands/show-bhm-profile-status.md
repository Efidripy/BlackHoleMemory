---
description: Show the current BHM runtime profile status for this workstation
---

Run the Windows helper below and report the current BHM profile-oriented runtime settings in a short summary:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\plugins\bhm-codex-connector\scripts\bhm-profile.ps1 -Action status -AsJson
```

Summarize:
- recommended profile
- current token budget
- current summarize chunk settings
- whether the plugin exposes a native UI toggle
