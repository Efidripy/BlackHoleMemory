"""Canonical BHM release-version manifest loading and validation."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


VERSION_MANIFEST_PATH = "config/version-manifest.json"
_SEMVER = re.compile(r"^\d+\.\d+\.\d+$")


@dataclass(frozen=True)
class VersionManifest:
    product: str
    release_version: str
    channel: str
    package_version: str
    runtime_version: str
    broker_version: str
    ui_version: str
    plugin_version: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "product": self.product,
            "release_version": self.release_version,
            "channel": self.channel,
            "components": {
                "package": self.package_version,
                "runtime": self.runtime_version,
                "broker": self.broker_version,
                "ui": self.ui_version,
                "plugin": self.plugin_version,
            },
        }


def _manifest_path(repo_root: Path | None = None) -> Path:
    root = repo_root or Path(__file__).resolve().parents[2]
    return root / VERSION_MANIFEST_PATH


def load_version_manifest(repo_root: Path | None = None) -> VersionManifest:
    path = _manifest_path(repo_root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid BHM version manifest: {path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("BHM version manifest schema_version must be 1")

    product = str(payload.get("product") or "").strip()
    release_version = str(payload.get("release_version") or "").strip()
    channel = str(payload.get("channel") or "").strip()
    components = payload.get("components")
    if not product or not channel or not _SEMVER.fullmatch(release_version):
        raise ValueError("BHM version manifest requires product, channel and semver release_version")
    if not isinstance(components, dict):
        raise ValueError("BHM version manifest components must be an object")

    values = {
        key: str(components.get(key) or "").strip()
        for key in ("package", "runtime", "broker", "ui", "plugin")
    }
    if values["package"] != release_version or values["plugin"] != release_version:
        raise ValueError("package and plugin versions must match release_version")
    for key, value in values.items():
        if not value or release_version not in value:
            raise ValueError(f"component version does not contain release_version: {key}")

    return VersionManifest(
        product=product,
        release_version=release_version,
        channel=channel,
        package_version=values["package"],
        runtime_version=values["runtime"],
        broker_version=values["broker"],
        ui_version=values["ui"],
        plugin_version=values["plugin"],
    )


VERSION_MANIFEST = load_version_manifest()
PACKAGE_VERSION = VERSION_MANIFEST.package_version
RUNTIME_VERSION = VERSION_MANIFEST.runtime_version
BROKER_VERSION = VERSION_MANIFEST.broker_version
UI_VERSION = VERSION_MANIFEST.ui_version
PLUGIN_VERSION = VERSION_MANIFEST.plugin_version


__all__ = [
    "BROKER_VERSION",
    "PACKAGE_VERSION",
    "PLUGIN_VERSION",
    "RUNTIME_VERSION",
    "UI_VERSION",
    "VERSION_MANIFEST",
    "VERSION_MANIFEST_PATH",
    "VersionManifest",
    "load_version_manifest",
]
