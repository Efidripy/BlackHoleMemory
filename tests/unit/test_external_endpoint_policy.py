from __future__ import annotations

import pytest

from blackholememory.external_endpoint_policy import PublicEndpointError
from blackholememory.external_endpoint_policy import validate_public_https_endpoint


@pytest.mark.parametrize(
    "url",
    [
        "http://api.example.com/search",
        "https://user:secret@example.com/search",
        "https://127.0.0.1/search",
        "https://[::1]/search",
        "https://224.0.0.1/search",
        "https://[ff02::1]/search",
        "https://100.64.0.1/search",
        "https://localhost/search",
        "https://localhost./search",
        "https://service.internal/search",
        "https://api.example.com/search#fragment",
    ],
)
def test_public_https_policy_rejects_unsafe_endpoints(url: str) -> None:
    with pytest.raises(PublicEndpointError):
        validate_public_https_endpoint(url)


def test_public_https_policy_accepts_public_https_endpoint() -> None:
    assert validate_public_https_endpoint("https://api.example.test/search") == "https://api.example.test/search"
