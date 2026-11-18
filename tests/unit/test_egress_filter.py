import time

import pytest

from blackholememory.config import Settings
from blackholememory.egress_filter import sanitize_for_egress
from blackholememory import write_enrichment


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


_NOTE = "Урок про SQLite и поиск, автор alice@example.invalid, token=abcdefgh12345678. " * 6


class _Resp:
    def raise_for_status(self):
        return None

    def json(self):
        content = '{"questions": ["Q1 bob@example.invalid?", "Q2?", "Q3?"]}'
        return {"choices": [{"message": {"content": content}}]}


def _client(sent):
    class Client:
        def __init__(self, **kwargs):
            sent["headers"] = kwargs.get("headers")

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def post(self, url, *, json, timeout):
            sent.update(url=url, payload=json, timeout=timeout)
            return _Resp()

    return Client


def _paid(monkeypatch, **kw):
    cfg = dict(llm_tier="paid", paid_llm_base_url="http://paid.test/v1", paid_llm_model="m", paid_llm_api_key="k")
    for key, value in {**cfg, **kw}.items():
        monkeypatch.setattr(write_enrichment.settings, key, value)
    monkeypatch.delenv("BHM_WRITE_DOC2QUERY", raising=False)


def test_default_tier_is_local():
    assert Settings().llm_tier == "local"


def test_local_tier_never_calls_out(monkeypatch):
    monkeypatch.delenv("BHM_WRITE_DOC2QUERY", raising=False)
    monkeypatch.setattr(write_enrichment.httpx, "Client", lambda **_: (_ for _ in ()).throw(AssertionError("no call")))
    assert write_enrichment.enrich_content("t", _NOTE) == _NOTE
    assert write_enrichment.schedule_live_enrichment({"content": _NOTE}) is None


def test_paid_tier_sends_only_filtered_text_with_auth(monkeypatch):
    sent = {}
    _paid(monkeypatch)
    monkeypatch.setattr(write_enrichment.httpx, "Client", _client(sent))
    out = write_enrichment.enrich_content("title bob@example.invalid", _NOTE)
    user = sent["payload"]["messages"][1]["content"]
    assert "alice@example.invalid" not in user and "bob@example.invalid" not in user and "abcdefgh12345678" not in user
    assert sent["headers"] == {"Authorization": "Bearer k"}
    assert "chat_template_kwargs" not in sent["payload"] and sent["timeout"] == 120.0
    assert sent["url"] == "http://paid.test/v1/chat/completions"
    assert out.startswith(_NOTE) and "Вопросы: Q1 [REDACTED:email]? Q2? Q3?" in out


def test_paid_tier_needs_complete_endpoint(monkeypatch):
    _paid(monkeypatch, paid_llm_api_key="")
    monkeypatch.setattr(write_enrichment.httpx, "Client", lambda **_: (_ for _ in ()).throw(AssertionError("no call")))
    assert write_enrichment.enrich_content("t", _NOTE) == _NOTE


def test_rerank_stays_local_without_its_own_opt_in(monkeypatch):
    from blackholememory import llm_rerank, paid_llm

    _paid(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK", "1")
    monkeypatch.setenv("BHM_LLM_RERANK_BASE_URL", "http://local.test/v1")
    monkeypatch.setenv("BHM_LLM_RERANK_MODEL", "qwen")
    monkeypatch.delenv("BHM_PAID_RERANK", raising=False)
    assert not paid_llm.rerank_uses_paid()
    assert llm_rerank._config()[:2] == ("http://local.test/v1", "qwen")
    assert "chat_template_kwargs" in paid_llm.local_chat_kwargs() and paid_llm.auth_headers() == {}
    assert paid_llm.outbound("a@example.invalid") == "a@example.invalid"


def test_rerank_paid_endpoint_auth_filter_and_no_qwen_kwargs(monkeypatch):
    from blackholememory import llm_rerank, paid_llm

    _paid(monkeypatch)
    monkeypatch.setenv("BHM_LLM_RERANK", "1")
    monkeypatch.setenv("BHM_PAID_RERANK", "1")
    assert paid_llm.rerank_uses_paid()
    assert llm_rerank._config()[:2] == ("http://paid.test/v1", "m")
    assert paid_llm.local_chat_kwargs() == {} and paid_llm.auth_headers() == {"Authorization": "Bearer k"}
    assert "[REDACTED:email]" in paid_llm.outbound("ask a@example.invalid")


def test_chat_endpoint_follows_tier_and_filters(monkeypatch):
    from blackholememory import paid_llm

    local = paid_llm.chat_endpoint()
    assert not local.paid and paid_llm.chat_outbound("a@example.invalid") == "a@example.invalid"
    _paid(monkeypatch)
    paid = paid_llm.chat_endpoint()
    assert paid[:3] == ("http://paid.test/v1", "m", "k") and paid.paid
    assert "[REDACTED:email]" in paid_llm.chat_outbound("a@example.invalid")


def test_gateway_payload_filters_and_drops_qwen_fields_for_paid_model():
    from blackholememory.llm_gateway import GatewayRequest, LocalOpenAICompatibleAdapter, ModelDefinition, PromptDefinition

    request = GatewayRequest(
        request_id="r", prompt_id="p", model_id="m",
        messages=({"role": "user", "content": "mail a@example.invalid token=abcdefgh12345678"},),
        chat_template_kwargs={"enable_thinking": False},
    )
    prompt = PromptDefinition("p", "1", "sys")
    paid = LocalOpenAICompatibleAdapter._payload(request, prompt, ModelDefinition("m", "http://x/v1", egress_filter=True))
    local = LocalOpenAICompatibleAdapter._payload(request, prompt, ModelDefinition("m", "http://x/v1"))
    assert "a@example.invalid" not in paid["messages"][1]["content"] and "abcdefgh12345678" not in paid["messages"][1]["content"]
    assert "chat_template_kwargs" not in paid and "chat_template_kwargs" in local
    assert "a@example.invalid" in local["messages"][1]["content"]


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("ФИО: Иванов Иван Иванович", "name"),
        ("Фамилия — Петров", "name"),
        ("Имя: Анна", "name"),
        ("Имя Анна", "name"),
        ("Отчество: Сергеевна", "name"),
        ("Full name: John Smith", "name"),
        ("Name: Jane Doe", "name"),
        ("Иванов Иван Иванович", "name"),
        ("Петрова Анна Ильинична", "name"),
        ("Адрес: Москва, ул. Ленина, д. 1", "address"),
        ("Address: 10 Main Street", "address"),
        ("Проживает в Москве на улице Ленина", "address"),
        ("Проживает:Москва", "address"),
        ("прописан по адресу Москва, дом 1", "address"),
        ("зарегистрирован по адресу Москва, дом 1", "address"),
        ("ул. Ленина, д. 12", "address"),
        ("проспект Мира дом 42", "address"),
        ("Дата рождения: 01.02.1990", "birthdate"),
        ("д.р. 01/02/1990", "birthdate"),
        ("DOB: 1990-02-01", "birthdate"),
        ("Date of birth - 01.02.1990", "birthdate"),
        ("born 1990-02-01", "birthdate"),
    ],
)
def test_names_addresses_and_birthdates_are_redacted(raw, kind):
    result = sanitize_for_egress(raw)
    assert result.text == f"[REDACTED:{kind}]"
    assert kind in result.kinds


@pytest.mark.parametrize("text", [
    "Class FooBar extends BaseHandler", "Иван", "Адрес сервера 127.0.0.1",
    "commit от 2026-11-18", "Name: sanitize_for_egress", "Москва",
    "Проект BlackHoleMemory использует функцию sanitize_for_egress и класс BaseHandler",
    "src/blackholememory/egress_filter.py version 1.2.3",
])
def test_engineering_text_and_unlabelled_words_are_untouched(text):
    assert sanitize_for_egress(text).text == text


def test_repeated_text_performance():
    text = "обычный текст ул. " * 1112
    start = time.perf_counter()
    sanitize_for_egress(text)
    assert time.perf_counter() - start < 1.0
