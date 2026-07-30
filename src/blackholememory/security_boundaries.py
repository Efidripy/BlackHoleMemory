"""Small, reusable security-boundary helpers.

These helpers keep filesystem inputs confined to an operator-approved root and
reject regex constructs that can turn bounded user input into an avoidable
backtracking workload.  They do not broaden permissions or perform I/O.
"""

from __future__ import annotations

import os
import re
from pathlib import Path


class SecurityBoundaryError(ValueError):
    """Raised when an input crosses a local security boundary."""


def resolve_under_root(root: Path, value: str | Path, *, require_leaf: bool = False) -> Path:
    """Resolve ``value`` and require it to remain under ``root``.

    Absolute paths are accepted only when they already point inside the
    approved root.  ``Path.resolve`` also closes the symlink escape variant.
    """

    raw = str(value or "").strip()
    if not raw:
        raise SecurityBoundaryError("path is required")
    base = os.path.realpath(os.fspath(root))
    raw_path = os.path.expanduser(raw)
    # Treat both slash styles as separators before asking the host OS to
    # resolve the path.  Otherwise ``..\\outside`` is a harmless filename on
    # POSIX while it is traversal on Windows, and a Windows drive path is
    # mistaken for a relative POSIX filename.
    portable_path = raw_path.replace("\\", "/")
    has_drive_prefix = bool(re.match(r"^[A-Za-z]:", portable_path))
    portable_absolute = portable_path.startswith("/") or has_drive_prefix
    if portable_absolute and not os.path.isabs(raw_path):
        raise SecurityBoundaryError("path must remain under the approved root")
    candidate_input = raw_path if portable_absolute else portable_path
    candidate = os.path.realpath(
        candidate_input if os.path.isabs(raw_path) else os.path.join(base, candidate_input)
    )
    try:
        common = os.path.commonpath((base, candidate))
    except ValueError as exc:
        raise SecurityBoundaryError("path must remain under the approved root") from exc
    if os.path.normcase(common) != os.path.normcase(base):
        raise SecurityBoundaryError("path must remain under the approved root")
    relative = os.path.relpath(candidate, base)
    if require_leaf and os.path.dirname(relative):
        raise SecurityBoundaryError("path must be a file name under the approved root")
    return Path(candidate)


def _has_nested_quantifier(pattern: str) -> bool:
    """Detect common nested-repeat shapes without depending on ``sre_parse``."""

    stack: list[tuple[bool, bool]] = []
    escaped = False
    in_class = False
    for index, char in enumerate(pattern):
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if char == "[" and not in_class:
            in_class = True
            continue
        if char == "]" and in_class:
            in_class = False
            continue
        if in_class:
            continue
        if char == "(":
            stack.append((False, False))
            continue
        if char == ")" and stack:
            has_quantifier, has_alternation = stack.pop()
            next_char = pattern[index + 1] if index + 1 < len(pattern) else ""
            if next_char in "*+?{" and (has_quantifier or has_alternation):
                return True
            if stack:
                parent_quantifier, parent_alternation = stack[-1]
                stack[-1] = (parent_quantifier or has_quantifier, parent_alternation or has_alternation)
            continue
        if stack:
            has_quantifier, has_alternation = stack[-1]
            if char == "|":
                stack[-1] = (has_quantifier, True)
            elif char in "*+?" or char == "{":
                stack[-1] = (True, has_alternation)
    return False


def compile_bounded_regex(pattern: str, *, field: str, max_length: int = 120) -> re.Pattern[str]:
    """Compile a caller-supplied regex after cheap, deterministic risk gates."""

    value = str(pattern or "")
    if not value:
        raise SecurityBoundaryError(f"{field} must not be empty")
    if len(value) > max_length:
        raise SecurityBoundaryError(f"{field} exceeds {max_length} characters")
    if re.search(r"\\[1-9]", value) or re.search(r"\(\?[=!<]", value):
        raise SecurityBoundaryError(f"{field} uses an unsupported backtracking construct")
    if value.count(".*") > 1 or value.count(".+") > 1 or _has_nested_quantifier(value):
        raise SecurityBoundaryError(f"{field} has unsafe nested repetition")
    # The public contract only needs bounded name alternatives.  Compile a
    # pattern made exclusively from escaped literals; accepting arbitrary
    # regex operators here would make the caller part of the regex program.
    alternatives = [part.strip() for part in value.split("|") if part.strip()]
    if not alternatives:
        raise SecurityBoundaryError(f"{field} must contain a non-empty literal")
    safe_literals = tuple(re.escape(part) for part in alternatives[:32])
    safe_source = "|".join(safe_literals)
    try:
        return re.compile(safe_source, re.IGNORECASE)
    except re.error as exc:
        raise SecurityBoundaryError(f"{field} is not a valid regular expression") from exc


__all__ = ["SecurityBoundaryError", "compile_bounded_regex", "resolve_under_root"]
