from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "audit-bhm-resource-call-sites.py"


def _load_inventory_module():
    spec = importlib.util.spec_from_file_location("bhm_resource_callsite_inventory", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_inventory_is_deterministic_and_limited_to_first_party_roots() -> None:
    module = _load_inventory_module()
    first = module.inventory(ROOT)
    second = module.inventory(ROOT)

    assert first == second
    assert first["ok"] is True
    assert first["scope"]["included_roots"] == ["src", "scripts"]
    assert first["scope"]["read_only"] is True
    assert len(first["inventory_sha256"]) == 64
    assert first["summary"]["call_sites"] > 0
    assert all(not row["path"].startswith("tests/") for row in first["call_sites"])


def test_inventory_classifies_process_filesystem_and_outbound_boundaries(tmp_path: Path) -> None:
    module = _load_inventory_module()
    source = tmp_path / "src" / "blackholememory"
    scripts = tmp_path / "scripts"
    source.mkdir(parents=True)
    scripts.mkdir()
    (source / "runtime.py").write_text(
        "import subprocess\nimport requests\nfrom pathlib import Path\n"
        "subprocess.run(['ok'], timeout=3)\n"
        "requests.get('http://127.0.0.1', timeout=1)\n"
        "Path('out').write_text('x')\n",
        encoding="utf-8",
    )
    (scripts / "validate-boundary.py").write_text(
        "import os\nos.remove('fixture')\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "fixture.py").parent.mkdir()
    (tmp_path / "tests" / "fixture.py").write_text("import os\nos.system('ignored')\n", encoding="utf-8")

    report = module.inventory(tmp_path)
    rows = report["call_sites"]

    assert report["summary"]["by_family"] == {
        "filesystem-call-sites": 2,
        "outbound-http-call-sites": 1,
        "process-execution-call-sites": 1,
    }
    assert {row["classification"] for row in rows} == {"runtime", "validator"}
    assert all(row["path"].startswith(("src/", "scripts/")) for row in rows)
    assert next(row for row in rows if row["callee"] == "subprocess.run")["explicit_budget"] is True
    assert next(row for row in rows if row["callee"] == "requests.get")["explicit_budget"] is True
