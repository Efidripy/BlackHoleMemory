from __future__ import annotations

import pytest

from blackholememory.context_profiles import load_context_profiles
from blackholememory.context_profiles import resolve_context_profile


def test_context_profiles_are_bounded_and_have_three_operational_modes():
    default, profiles = load_context_profiles()

    assert default == "standard"
    assert set(profiles) == {"low-context", "standard", "deep"}
    assert profiles["low-context"].token_budget < profiles["standard"].token_budget < profiles["deep"].token_budget
    assert profiles["low-context"].limit < profiles["standard"].limit < profiles["deep"].limit
    assert all(64 <= profile.token_budget <= 8000 for profile in profiles.values())
    assert all(1 <= profile.limit <= 50 for profile in profiles.values())


def test_context_profile_aliases_resolve_to_canonical_names():
    assert resolve_context_profile("low").name == "low-context"
    assert resolve_context_profile("standard_context").name == "standard"
    assert resolve_context_profile("deep-context").name == "deep"


def test_unknown_context_profile_fails_closed():
    with pytest.raises(ValueError, match="unknown context profile"):
        resolve_context_profile("experimental")
