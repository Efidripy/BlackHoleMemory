from __future__ import annotations

from collections import Counter
from pathlib import Path

import pytest

from .domain_manifest import DOMAIN_NAMES, classify_test, marker_for


INTEGRATION_ROOT = Path(__file__).resolve().parent


def pytest_configure(config: pytest.Config) -> None:
    for domain in DOMAIN_NAMES:
        config.addinivalue_line(
            "markers",
            f"{marker_for(domain)}: BHM integration domain: {domain}",
        )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    counts: Counter[str] = Counter()
    for item in items:
        try:
            item.path.relative_to(INTEGRATION_ROOT)
        except ValueError:
            continue
        file_name = item.path.name
        test_name = item.name.split("[", 1)[0]
        try:
            domain = classify_test(file_name, test_name)
        except ValueError as exc:
            raise pytest.UsageError(str(exc)) from exc
        item.add_marker(getattr(pytest.mark, marker_for(domain)))
        counts[domain] += 1
    config._bhm_integration_domain_counts = dict(sorted(counts.items()))


def pytest_report_header(config: pytest.Config) -> str:
    counts = getattr(config, "_bhm_integration_domain_counts", {})
    if not counts:
        return "BHM integration domains: collecting"
    rendered = ", ".join(f"{domain}={count}" for domain, count in counts.items())
    return f"BHM integration domains: {rendered}"


def pytest_terminal_summary(terminalreporter: pytest.TerminalReporter) -> None:
    counts = getattr(terminalreporter.config, "_bhm_integration_domain_counts", {})
    if counts:
        rendered = ", ".join(f"{domain}={count}" for domain, count in counts.items())
        terminalreporter.write_line(f"BHM integration domain partition: {rendered}")
