from types import SimpleNamespace

import pytest

from blackholememory.config import Settings
from blackholememory.egress_filter import sanitize_for_egress
from blackholememory.llm_enhancements import expand_queries, paid_tier_ready


@pytest.mark.parametrize(
    "raw",
    [
        "write to john.doe@example.invalid today",
        "call +7 916 123-45-67 now",
        "call 8 (916) 123-45-67 now",
        "card 4111 1111 1111 1111 expired",
        "iban GB82WEST12345698765432 here",
        "ssn 123-45-6789 leaked",
        "ИНН: 7707083893 организации",
        "паспорт 4510 123456 выдан",
        "client ip 8.8.4.4 logged",
        "OPENAI_API_KEY=abcdefghijklmnop1234",
    ],
)
def test_pii_and_secrets_are_redacted(raw):
    result = sanitize_for_egress(raw)
    assert "[REDACTED:" in result.text
    assert result.kinds


def test_ordinary_engineering_text_is_untouched():
    text = "Fixed retrieval at 127.0.0.1 and 192.168.1.5 on 2026-11-18, build 4.5.1, id 4111 1111 1111 1112"
    assert sanitize_for_egress(text).text == text  # private IPs, dates, versions, bad-Luhn number


def _settings(**kw):
    base = dict(llm_tier="paid", paid_llm_base_url="http://x/v1", paid_llm_model="m", paid_llm_api_key="k")
    return SimpleNamespace(**{**base, **kw})


def test_default_tier_is_local_and_never_calls_out():
    assert Settings().llm_tier == "local"

    def boom(*a):
        raise AssertionError("no request in local tier")

    assert not paid_tier_ready(_settings(llm_tier="local"))
    assert expand_queries("note", settings=_settings(llm_tier="local"), transport=boom) == []
    assert expand_queries("note", settings=_settings(paid_llm_api_key=""), transport=boom) == []


def test_expand_queries_sends_only_sanitized_text_and_cleans_output():
    sent = {}

    def transport(url, payload, headers, timeout):
        sent["user"] = payload["messages"][1]["content"]
        content = '{"questions": ["how to mail bob@example.invalid?", "  ", 5, "q2"]}'
        return {"choices": [{"message": {"content": content}}]}

    out = expand_queries("contact alice@example.invalid, token=abcdefgh12345678", settings=_settings(), transport=transport)
    assert "alice@example.invalid" not in sent["user"] and "abcdefgh12345678" not in sent["user"]
    assert out == ["how to mail [REDACTED:email]?", "q2"]


def test_expand_queries_failure_returns_empty():
    def transport(*a):
        raise OSError("down")

    assert expand_queries("note", settings=_settings(), transport=transport) == []
    assert expand_queries("note", settings=_settings(), transport=lambda *a: {"choices": []}) == []
