"""Manage the local capacity-aware Codex Security preflight overlay.

The overlay deliberately changes only ``preflight/capability-profiles.toml``.
It does not patch the Deep Security Scan skill or weaken its contract requiring
exactly six independent discovery outputs per completed round.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_POLICY_PATH = REPO_ROOT / ".docs" / "config" / "security-scan-capacity.json"
PROFILE_RELATIVE_PATH = Path("preflight/capability-profiles.toml")
DEFAULT_BACKUP_SUFFIX = ".bhm-capacity-overlay.bak"

CAPABILITY_1 = """[capabilities.usable_worker_slots_1]
kind = "multi_agent_capacity"
op = ">="
value = 1
v1_default = 6"""

CAPABILITY_6 = """[capabilities.usable_worker_slots_6]
kind = "multi_agent_capacity"
op = ">="
value = 6
v1_default = 6"""

DEEP_6_BLOCK = '''[[profiles.deep_security_scan.requirements]]
capability = "usable_worker_slots_6"
severity = "block"
reason = "Each completed discovery round requires exactly six usable workers."'''

DEEP_1_BLOCK = '''[[profiles.deep_security_scan.requirements]]
capability = "usable_worker_slots_1"
severity = "block"
reason = "Deep scan requires at least one usable delegated worker; six independent outputs may run in capacity-aware waves."'''

DEEP_6_WARN = '''[[profiles.deep_security_scan.requirements]]
capability = "usable_worker_slots_6"
severity = "warn"
reason = "Six usable workers complete one discovery round in a single wave; smaller capacities must use independent sequential waves."'''

DEEP_8_WARN = '''[[profiles.deep_security_scan.requirements]]
capability = "usable_worker_slots_8"
severity = "warn"
reason = "Eight threads leaves headroom beyond the six required discovery workers for coordinator and nested work."'''


class OverlayError(RuntimeError):
    """Raised when a guarded overlay operation cannot proceed safely."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _load_policy(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    policy = json.loads(raw.decode("utf-8"))
    expected = {
        "required_independent_outputs_per_round": 6,
        "coordinator_reserve_slots": 1,
        "minimum_usable_delegated_worker_slots": 1,
        "maximum_simultaneous_discovery_workers": 6,
        "recommended_usable_worker_slots_with_headroom": 8,
        "scheduling_mode": "capacity_aware_waves",
        "zero_usable_worker_slots_disposition": "blocked",
    }
    mismatches = {
        key: {"expected": value, "actual": policy.get(key)}
        for key, value in expected.items()
        if policy.get(key) != value
    }
    expected_independence = [
        "fresh_worker_thread_per_output",
        "fork_turns_none",
        "same_canonical_brief",
        "shared_frozen_worklists",
        "no_prior_worker_results",
        "separate_worker_artifacts",
        "metadata_only_partial_inspection",
        "merge_only_after_all_required_outputs_complete",
    ]
    if policy.get("worker_independence_requirements") != expected_independence:
        mismatches["worker_independence_requirements"] = {
            "expected": expected_independence,
            "actual": policy.get("worker_independence_requirements"),
        }
    overlay = policy.get("overlay")
    if not isinstance(overlay, dict):
        mismatches["overlay"] = {"expected": "object", "actual": type(overlay).__name__}
    else:
        if overlay.get("target_relative_path") != PROFILE_RELATIVE_PATH.as_posix():
            mismatches["overlay.target_relative_path"] = {
                "expected": PROFILE_RELATIVE_PATH.as_posix(),
                "actual": overlay.get("target_relative_path"),
            }
        if overlay.get("backup_suffix") != DEFAULT_BACKUP_SUFFIX:
            mismatches["overlay.backup_suffix"] = {
                "expected": DEFAULT_BACKUP_SUFFIX,
                "actual": overlay.get("backup_suffix"),
            }
        prohibited = overlay.get("prohibited_targets", [])
        if "skills/deep-security-scan/SKILL.md" not in prohibited:
            mismatches["overlay.prohibited_targets"] = {
                "expected": ["skills/deep-security-scan/SKILL.md"],
                "actual": prohibited,
            }
    if mismatches:
        raise OverlayError(f"capacity policy contract mismatch: {json.dumps(mismatches, sort_keys=True)}")
    return policy, _sha256(raw)


def _schedule_matrix(policy: dict[str, Any]) -> list[dict[str, Any]]:
    required = int(policy["required_independent_outputs_per_round"])
    reserve = int(policy["coordinator_reserve_slots"])
    maximum = int(policy["maximum_simultaneous_discovery_workers"])
    recommended = int(policy["recommended_usable_worker_slots_with_headroom"])
    minimum = int(policy["minimum_usable_delegated_worker_slots"])
    highest_total_capacity = recommended + reserve
    matrix: list[dict[str, Any]] = []
    for total_capacity in range(1, highest_total_capacity + 1):
        usable = max(0, total_capacity - reserve)
        if usable < minimum:
            waves: list[int] = []
            status = str(policy["zero_usable_worker_slots_disposition"])
        else:
            wave_size = min(required, maximum, usable)
            remaining = required
            waves = []
            while remaining:
                current = min(wave_size, remaining)
                waves.append(current)
                remaining -= current
            status = "ready"
        matrix.append(
            {
                "total_capacity": total_capacity,
                "coordinator_reserve": reserve,
                "usable_delegated_worker_slots": usable,
                "status": status,
                "waves": waves,
                "independent_outputs": sum(waves),
                "recommended_headroom_met": usable >= recommended,
            }
        )
    return matrix


def _normalize_newlines(text: str) -> tuple[str, str]:
    newline = "\r\n" if "\r\n" in text else "\n"
    return text.replace("\r\n", "\n"), newline


def _restore_newlines(text: str, newline: str) -> str:
    return text if newline == "\n" else text.replace("\n", "\r\n")


def _classify_profile(text: str) -> str:
    normalized, _newline = _normalize_newlines(text)
    upstream = (
        CAPABILITY_1 not in normalized
        and normalized.count(CAPABILITY_6) == 1
        and normalized.count(DEEP_6_BLOCK) == 1
        and normalized.count(DEEP_8_WARN) == 1
    )
    applied = (
        normalized.count(CAPABILITY_1) == 1
        and normalized.count(CAPABILITY_6) == 1
        and normalized.count(DEEP_1_BLOCK) == 1
        and normalized.count(DEEP_6_WARN) == 1
        and normalized.count(DEEP_8_WARN) == 1
        and DEEP_6_BLOCK not in normalized
    )
    if upstream:
        return "upstream"
    if applied:
        return "applied"
    return "unknown"


def _apply_overlay_text(text: str) -> str:
    state = _classify_profile(text)
    if state == "applied":
        return text
    if state != "upstream":
        raise OverlayError("capability profile does not match the guarded upstream contract")
    normalized, newline = _normalize_newlines(text)
    patched = normalized.replace(CAPABILITY_6, f"{CAPABILITY_1}\n\n{CAPABILITY_6}", 1)
    patched = patched.replace(DEEP_6_BLOCK, f"{DEEP_1_BLOCK}\n\n{DEEP_6_WARN}", 1)
    if _classify_profile(patched) != "applied":
        raise OverlayError("generated overlay failed its structural verification")
    return _restore_newlines(patched, newline)


def _resolve_plugin_root(explicit: Path | None) -> Path:
    if explicit is not None:
        candidates = [explicit.expanduser().resolve()]
    else:
        configured = os.environ.get("BHM_CODEX_SECURITY_PLUGIN_ROOT")
        if configured:
            candidates = [Path(configured).expanduser().resolve()]
        else:
            codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
            candidates = [codex_home / "plugins" / "cache" / "openai-curated" / "codex-security"]

    resolved: list[Path] = []
    for candidate in candidates:
        if (candidate / PROFILE_RELATIVE_PATH).is_file():
            resolved.append(candidate)
            continue
        if candidate.is_dir():
            resolved.extend(
                child
                for child in sorted(candidate.iterdir())
                if child.is_dir() and (child / PROFILE_RELATIVE_PATH).is_file()
            )
    unique = sorted({path.resolve() for path in resolved}, key=lambda path: str(path).lower())
    if not unique:
        raise OverlayError("Codex Security plugin root was not found")
    if len(unique) != 1:
        rendered = [str(path) for path in unique]
        raise OverlayError(f"multiple Codex Security plugin roots found; pass --plugin-root: {rendered}")
    return unique[0]


def _read_utf8(path: Path) -> tuple[bytes, str]:
    raw = path.read_bytes()
    try:
        return raw, raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OverlayError(f"expected UTF-8 profile: {path}") from error


def _atomic_replace(path: Path, data: bytes, *, expected_current_hash: str) -> None:
    current = path.read_bytes()
    if _sha256(current) != expected_current_hash:
        raise OverlayError("target changed after inspection; refusing concurrent overwrite")
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(path.stat().st_mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _create_backup(path: Path, data: bytes, *, source_path: Path, expected_source_hash: str) -> None:
    if _sha256(source_path.read_bytes()) != expected_source_hash:
        raise OverlayError("target changed before backup creation; refusing apply")
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as error:
        raise OverlayError("backup appeared concurrently; rerun status before apply") from error


def _base_result(
    *,
    action: str,
    plugin_root: Path,
    policy_path: Path,
    policy_hash: str,
    policy: dict[str, Any],
) -> dict[str, Any]:
    profiles_path = plugin_root / PROFILE_RELATIVE_PATH
    backup_path = profiles_path.with_name(profiles_path.name + DEFAULT_BACKUP_SUFFIX)
    return {
        "schema_version": "bhm.codex-security.capacity-overlay-result.v1",
        "action": action,
        "plugin_root": str(plugin_root),
        "profiles_path": str(profiles_path),
        "backup_path": str(backup_path),
        "policy_path": str(policy_path),
        "policy_hash": policy_hash,
        "skill_file_patched": False,
        "schedule_matrix": _schedule_matrix(policy),
    }


def _inspect(
    *, action: str, plugin_root: Path, policy_path: Path, policy_hash: str, policy: dict[str, Any]
) -> tuple[dict[str, Any], bytes, str, Path, bytes | None]:
    result = _base_result(
        action=action,
        plugin_root=plugin_root,
        policy_path=policy_path,
        policy_hash=policy_hash,
        policy=policy,
    )
    profiles_path = Path(result["profiles_path"])
    backup_path = Path(result["backup_path"])
    current_raw, current_text = _read_utf8(profiles_path)
    backup_raw = backup_path.read_bytes() if backup_path.is_file() else None
    result.update(
        {
            "profile_state": _classify_profile(current_text),
            "source_hash_before": _sha256(current_raw),
            "source_hash_after": _sha256(current_raw),
            "backup_hash": _sha256(backup_raw) if backup_raw is not None else None,
            "backup_exists": backup_raw is not None,
        }
    )
    return result, current_raw, current_text, backup_path, backup_raw


def _status(plugin_root: Path, policy_path: Path, policy_hash: str, policy: dict[str, Any]) -> dict[str, Any]:
    result, current_raw, current_text, _backup_path, backup_raw = _inspect(
        action="status",
        plugin_root=plugin_root,
        policy_path=policy_path,
        policy_hash=policy_hash,
        policy=policy,
    )
    state = str(result["profile_state"])
    if state == "upstream":
        backup_matches = backup_raw is None or backup_raw == current_raw
        result.update(
            {
                "ok": backup_matches,
                "managed_state": "ready_to_apply" if backup_matches else "stale_backup",
                "safe_to_apply": backup_matches,
                "safe_to_rollback": backup_raw == current_raw if backup_raw is not None else True,
                "expected_overlay_hash": _sha256(_apply_overlay_text(current_text).encode("utf-8")),
                "changed": False,
            }
        )
        return result
    if state == "applied":
        backup_matches = False
        expected_overlay_hash = None
        if backup_raw is not None:
            try:
                backup_text = backup_raw.decode("utf-8")
                expected_overlay_hash = _sha256(_apply_overlay_text(backup_text).encode("utf-8"))
                backup_matches = expected_overlay_hash == _sha256(current_raw)
            except (UnicodeDecodeError, OverlayError):
                backup_matches = False
        result.update(
            {
                "ok": backup_matches,
                "managed_state": "applied" if backup_matches else "applied_without_valid_backup",
                "safe_to_apply": backup_matches,
                "safe_to_rollback": backup_matches,
                "expected_overlay_hash": expected_overlay_hash,
                "changed": False,
            }
        )
        return result
    result.update(
        {
            "ok": False,
            "managed_state": "unsupported_profile_drift",
            "safe_to_apply": False,
            "safe_to_rollback": False,
            "expected_overlay_hash": None,
            "changed": False,
        }
    )
    return result


def _apply(plugin_root: Path, policy_path: Path, policy_hash: str, policy: dict[str, Any]) -> dict[str, Any]:
    result, current_raw, current_text, backup_path, backup_raw = _inspect(
        action="apply",
        plugin_root=plugin_root,
        policy_path=policy_path,
        policy_hash=policy_hash,
        policy=policy,
    )
    current_hash = _sha256(current_raw)
    state = str(result["profile_state"])
    if state == "applied":
        if backup_raw is None:
            raise OverlayError("overlay is already present but no rollback backup exists")
        expected = _apply_overlay_text(backup_raw.decode("utf-8")).encode("utf-8")
        if _sha256(expected) != current_hash:
            raise OverlayError("applied overlay does not match the adjacent backup")
        result.update(
            {
                "ok": True,
                "managed_state": "applied",
                "expected_overlay_hash": current_hash,
                "changed": False,
            }
        )
        return result
    if state != "upstream":
        raise OverlayError("unsupported capability profile drift; refusing apply")
    if backup_raw is None:
        _create_backup(backup_path, current_raw, source_path=Path(result["profiles_path"]), expected_source_hash=current_hash)
        backup_raw = current_raw
    elif backup_raw != current_raw:
        raise OverlayError("adjacent backup does not match the current upstream profile")
    patched_raw = _apply_overlay_text(current_text).encode("utf-8")
    patched_hash = _sha256(patched_raw)
    _atomic_replace(Path(result["profiles_path"]), patched_raw, expected_current_hash=current_hash)
    result.update(
        {
            "ok": True,
            "profile_state": "applied",
            "managed_state": "applied",
            "source_hash_after": patched_hash,
            "backup_hash": _sha256(backup_raw),
            "backup_exists": True,
            "expected_overlay_hash": patched_hash,
            "changed": True,
        }
    )
    return result


def _rollback(plugin_root: Path, policy_path: Path, policy_hash: str, policy: dict[str, Any]) -> dict[str, Any]:
    result, current_raw, _current_text, _backup_path, backup_raw = _inspect(
        action="rollback",
        plugin_root=plugin_root,
        policy_path=policy_path,
        policy_hash=policy_hash,
        policy=policy,
    )
    current_hash = _sha256(current_raw)
    state = str(result["profile_state"])
    if state == "upstream":
        if backup_raw is not None and backup_raw != current_raw:
            raise OverlayError("current upstream profile and adjacent backup differ; refusing rollback")
        result.update(
            {
                "ok": True,
                "managed_state": "rolled_back",
                "changed": False,
                "expected_overlay_hash": _sha256(_apply_overlay_text(current_raw.decode("utf-8")).encode("utf-8")),
            }
        )
        return result
    if state != "applied":
        raise OverlayError("unsupported capability profile drift; refusing rollback")
    if backup_raw is None:
        raise OverlayError("rollback backup is missing")
    try:
        backup_text = backup_raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise OverlayError("rollback backup is not UTF-8") from error
    if _classify_profile(backup_text) != "upstream":
        raise OverlayError("rollback backup does not match the guarded upstream contract")
    expected_overlay = _apply_overlay_text(backup_text).encode("utf-8")
    expected_overlay_hash = _sha256(expected_overlay)
    if expected_overlay_hash != current_hash:
        raise OverlayError("current overlay hash drift does not match the rollback backup")
    _atomic_replace(Path(result["profiles_path"]), backup_raw, expected_current_hash=current_hash)
    result.update(
        {
            "ok": True,
            "profile_state": "upstream",
            "managed_state": "rolled_back",
            "source_hash_after": _sha256(backup_raw),
            "expected_overlay_hash": expected_overlay_hash,
            "changed": True,
        }
    )
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "apply", "rollback"))
    parser.add_argument("--plugin-root", type=Path, help="Exact plugin root or its version-parent directory.")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY_PATH)
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        policy_path = args.policy.expanduser().resolve()
        policy, policy_hash = _load_policy(policy_path)
        plugin_root = _resolve_plugin_root(args.plugin_root)
        handlers = {"status": _status, "apply": _apply, "rollback": _rollback}
        result = handlers[args.action](plugin_root, policy_path, policy_hash, policy)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if result.get("ok") else 2
    except (OSError, ValueError, json.JSONDecodeError, OverlayError) as error:
        print(
            json.dumps(
                {
                    "schema_version": "bhm.codex-security.capacity-overlay-result.v1",
                    "action": args.action,
                    "ok": False,
                    "error": type(error).__name__,
                    "message": str(error),
                },
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
