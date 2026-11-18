"""Outbound filter for text that may leave the machine to a paid LLM.

Secrets are removed by ``observation_security.redact_secret_text``; this module adds a
standard regex layer for personal data, including labelled names, addresses and birth
dates and selected Russian name and address patterns. This is not NER and does not
detect every name or address in plain prose.
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .observation_security import redact_secret_text


@dataclass(frozen=True)
class EgressResult:
    text: str
    kinds: tuple[str, ...]


def _luhn_ok(digits: str) -> bool:
    total = 0
    for index, char in enumerate(reversed(digits)):
        value = int(char)
        if index % 2:
            value = value * 2 - 9 if value > 4 else value * 2
        total += value
    return total % 10 == 0


def _iban_ok(value: str) -> bool:
    rearranged = value[4:] + value[:4]
    number = "".join(str(int(c, 36)) for c in rearranged)
    return int(number) % 97 == 1


_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
_PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}[\s.-]?\(?\d{2,4}\)?[\s.-]?\d{2,4}[\s.-]?\d{2,4}(?:[\s.-]?\d{2,4})?(?!\d)")
_PHONE_RU = re.compile(r"(?<![\w+])8[\s(-]*\d{3}[\s)-]*\d{3}[\s-]*\d{2}[\s-]*\d{2}(?!\d)")
_CARD = re.compile(r"(?<![\d-])\d(?:[ -]?\d){12,18}(?![\d-])")
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
_SSN = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_LABELLED_ID = re.compile(
    r"(?i)\b(ИНН|СНИЛС|ОГРН|паспорт|passport|SSN|ID\s*card)\b(\s*(?:№|N|No\.?|:|=)?\s*)[\d][\d\s-]{5,}\d"
)
_IPV4 = re.compile(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])")


# Bound each field and keep case-insensitive matching confined to labels and suffixes.
_NAME_WORD = r"[A-ZА-ЯЁ][a-zа-яё]+(?:[-'][A-ZА-ЯЁa-zа-яё]+)*"
_LABELLED_NAME = re.compile(
    rf"(?<!\w)(?:(?i:ФИО|Фамилия|Имя|Отчество|Full name)"
    rf"(?:[ \t]*[:—–-][ \t]*|[ \t]+)|(?i:Name)[ \t]*[:—–-][ \t]*)"
    rf"{_NAME_WORD}(?:[ \t]+{_NAME_WORD}){{0,2}}(?![\w-])"
)
_RUSSIAN_NAME = re.compile(
    r"(?<!\w)[А-ЯЁ][а-яё]+[ \t]+[А-ЯЁ][а-яё]+[ \t]+"
    r"[А-ЯЁ][а-яё]*(?i:ович|евич|ьич|овна|евна|ична|инична)(?!\w)"
)
_LABELLED_ADDRESS = re.compile(
    r"(?<!\w)(?:(?i:Адрес|Address)[ \t]*[:—–-][ \t]*|"
    r"(?i:Проживает|прописан(?:а)?[ \t]+по[ \t]+адресу|"
    r"зарегистрирован(?:а)?[ \t]+по[ \t]+адресу)"
    r"(?:[ \t]*[:—–-][ \t]*|[ \t]+))"
    r"[^\r\n]{1,120}"
)
_STREET_ADDRESS = re.compile(
    r"(?<!\w)(?i:ул\.|улица|пр\.|проспект|пер\.|шоссе)[ \t]+"
    r"[^\r\n]{1,100}?[ \t,]+(?i:д\.|дом)[ \t]*\d+[\w/-]*"
)
_BIRTHDATE = re.compile(
    r"(?<!\w)(?i:Дата[ \t]+рождения|д\.р\.|DOB|Date[ \t]+of[ \t]+birth|born)"
    r"[ \t]*[:—–-]?[ \t]*(?:\d{2}\.\d{2}\.\d{4}|\d{4}-\d{2}-\d{2}|"
    r"\d{2}/\d{2}/\d{4})(?!\d)"
)


def _sub_card(match: re.Match[str]) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    return "[REDACTED:card]" if 13 <= len(digits) <= 19 and _luhn_ok(digits) else match.group(0)


def _sub_iban(match: re.Match[str]) -> str:
    try:
        return "[REDACTED:iban]" if _iban_ok(match.group(0)) else match.group(0)
    except ValueError:
        return match.group(0)


def _sub_ip(match: re.Match[str]) -> str:
    try:
        ip = ipaddress.ip_address(match.group(0))
    except ValueError:
        return match.group(0)
    # Private/loopback addresses are ordinary dev context, public ones can identify a person.
    return match.group(0) if ip.is_private or ip.is_loopback or ip.is_link_local else "[REDACTED:ip]"


_RULES = (
    (_BIRTHDATE, "[REDACTED:birthdate]", "birthdate"),
    (_LABELLED_ADDRESS, "[REDACTED:address]", "address"),
    (_STREET_ADDRESS, "[REDACTED:address]", "address"),
    (_LABELLED_NAME, "[REDACTED:name]", "name"),
    (_RUSSIAN_NAME, "[REDACTED:name]", "name"),
    (_EMAIL, "[REDACTED:email]", "email"),
    (_LABELLED_ID, lambda m: f"{m.group(1)}{m.group(2)}[REDACTED:national-id]", "national-id"),
    (_SSN, "[REDACTED:national-id]", "national-id"),
    (_CARD, _sub_card, "card"),
    (_IBAN, _sub_iban, "iban"),
    (_PHONE_INTL, "[REDACTED:phone]", "phone"),
    (_PHONE_RU, "[REDACTED:phone]", "phone"),
    (_IPV4, _sub_ip, "ip"),
)


def sanitize_for_egress(text: str) -> EgressResult:
    """Return ``text`` with secrets and common personal-data patterns replaced by markers."""

    secret = redact_secret_text(text)
    value = secret.value
    kinds = list(secret.kinds)
    for pattern, replacement, kind in _RULES:
        before = value
        value, count = pattern.subn(replacement, value)
        # Callables may return the original text (failed checksum); report real changes only.
        if value != before:
            kinds.extend([kind] * count)
    return EgressResult(text=value, kinds=tuple(kinds))
