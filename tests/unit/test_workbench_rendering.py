from __future__ import annotations

import re
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WORKBENCH_HTML = REPO_ROOT / "plugins" / "bhm-codex-connector" / "ui" / "bhm-workbench.html"


def test_workbench_uses_dom_construction_for_untrusted_values(tmp_path: Path) -> None:
    source = WORKBENCH_HTML.read_text(encoding="utf-8")

    for forbidden in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write", "escapeHtml"):
        assert forbidden not in source
    assert "replaceChildren" in source
    assert ".textContent" in source
    assert 'button.dataset[datasetKey] = String(value ?? "")' in source

    scripts = re.findall(r"<script>(.*?)</script>", source, flags=re.DOTALL | re.IGNORECASE)
    assert len(scripts) == 1
    script_path = tmp_path / "bhm-workbench-inline.js"
    script_path.write_text(scripts[0], encoding="utf-8")
    completed = subprocess.run(
        ["node", "--check", str(script_path)],
        check=False,
        text=True,
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 0, completed.stderr


def test_workbench_api_uses_fragment_bootstrap_and_bearer_header() -> None:
    source = WORKBENCH_HTML.read_text(encoding="utf-8")

    assert 'params.get("bhm-workbench-capability")' in source
    assert "window.history.replaceState" in source
    assert 'headers.set("Authorization", `Bearer ${workbenchCapability}`)' in source
    assert source.count("fetch(") == 1
    assert "return fetch(path" in source
