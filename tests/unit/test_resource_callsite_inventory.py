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
    assert all(
        row["explicit_budget"]
        for row in first["call_sites"]
        if row["operation"] == "process-run"
    )
    assert all(
        row["explicit_budget"]
        for row in first["call_sites"]
        if row["operation"] == "transport"
    )
    lifecycle_rows = [
        row
        for row in first["call_sites"]
        if row["operation"] in {"process-lifecycle", "process-replacement"}
    ]
    assert lifecycle_rows
    assert first["summary"]["lifecycle_rows"] == len(lifecycle_rows)
    assert first["summary"]["lifecycle_verified_rows"] == len(lifecycle_rows)
    assert first["summary"]["lifecycle_unresolved_rows"] == []
    assert first["summary"]["lifecycle_coverage_ok"] is True
    assert all(row["lifecycle_disposition"] for row in lifecycle_rows)
    assert all(row["lifecycle_evidence"] for row in lifecycle_rows)
    assert all(row["lifecycle_verified"] for row in lifecycle_rows)
    mutation_rows = [row for row in first["call_sites"] if row["operation"] == "filesystem-mutation"]
    assert mutation_rows
    assert first["summary"]["mutation_rows"] == len(mutation_rows)
    assert first["summary"]["mutation_verified_rows"] == len(mutation_rows)
    assert first["summary"]["mutation_unresolved_rows"] == []
    assert first["summary"]["mutation_coverage_ok"] is True
    assert all(row["mutation_disposition"] for row in mutation_rows)
    assert all(row["mutation_evidence"] for row in mutation_rows)
    assert all(row["mutation_verified"] for row in mutation_rows)
    outbound_rows = [row for row in first["call_sites"] if row["family"] == "outbound-http-call-sites"]
    assert outbound_rows
    assert first["summary"]["outbound_rows"] == len(outbound_rows)
    assert first["summary"]["outbound_verified_rows"] == len(outbound_rows)
    assert first["summary"]["outbound_unresolved_rows"] == []
    assert first["summary"]["outbound_coverage_ok"] is True
    assert all(row["outbound_disposition"] for row in outbound_rows)
    assert all(row["outbound_evidence"] for row in outbound_rows)
    assert all(row["outbound_verified"] for row in outbound_rows)


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
    assert next(row for row in rows if row["callee"] == "requests.get")["operation"] == "transport"
    assert next(row for row in rows if row["callee"] == "subprocess.run")["operation"] == "process-run"
    assert {row["scope"] for row in rows} == {"<module>"}


def test_unmapped_process_lifecycle_row_remains_explicitly_unresolved(tmp_path: Path) -> None:
    module = _load_inventory_module()
    source = tmp_path / "src" / "blackholememory"
    source.mkdir(parents=True)
    (source / "runtime.py").write_text(
        "import subprocess\n\ndef start():\n    return subprocess.Popen(['ok'])\n",
        encoding="utf-8",
    )

    report = module.inventory(tmp_path)
    row = next(row for row in report["call_sites"] if row["operation"] == "process-lifecycle")

    assert row["scope"] == "start"
    assert row["lifecycle_disposition"] is None
    assert row["lifecycle_evidence"] == ()
    assert row["lifecycle_verified"] is False
    assert report["summary"]["lifecycle_rows"] == 1
    assert report["summary"]["lifecycle_verified_rows"] == 0
    assert report["summary"]["lifecycle_coverage_ok"] is False
    assert report["summary"]["lifecycle_unresolved_rows"] == [
        {"path": "src/blackholememory/runtime.py", "line": 4, "callee": "subprocess.Popen", "scope": "start"}
    ]


def test_unmapped_filesystem_mutation_remains_explicitly_unresolved(tmp_path: Path) -> None:
    module = _load_inventory_module()
    source = tmp_path / "src" / "blackholememory"
    source.mkdir(parents=True)
    (source / "runtime.py").write_text(
        "from pathlib import Path\n\ndef write():\n    return Path('out').write_text('x')\n",
        encoding="utf-8",
    )

    report = module.inventory(tmp_path)
    row = next(row for row in report["call_sites"] if row["operation"] == "filesystem-mutation")

    assert row["scope"] == "write"
    assert row["mutation_disposition"] is None
    assert row["mutation_evidence"] == ()
    assert row["mutation_verified"] is False
    assert report["summary"]["mutation_rows"] == 1
    assert report["summary"]["mutation_verified_rows"] == 0
    assert report["summary"]["mutation_coverage_ok"] is False


def test_unbounded_httpx_constructor_remains_explicitly_unresolved(tmp_path: Path) -> None:
    module = _load_inventory_module()
    source = tmp_path / "src" / "blackholememory"
    source.mkdir(parents=True)
    (source / "runtime.py").write_text(
        "import httpx\n\ndef request():\n    return httpx.Client(timeout=2)\n",
        encoding="utf-8",
    )

    report = module.inventory(tmp_path)
    row = next(row for row in report["call_sites"] if row["family"] == "outbound-http-call-sites")

    assert row["outbound_disposition"] == "bounded-httpx-client"
    assert row["outbound_verified"] is False
    assert report["summary"]["outbound_rows"] == 1
    assert report["summary"]["outbound_verified_rows"] == 0
    assert report["summary"]["outbound_coverage_ok"] is False
