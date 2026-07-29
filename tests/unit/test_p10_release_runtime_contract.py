from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
BUILDER = REPO_ROOT / "scripts" / "build-release.ps1"
VERIFIER = REPO_ROOT / "scripts" / "verify-release-build.py"
POSTINSTALL = REPO_ROOT / "scripts" / "validate-bhm-release-postinstall.ps1"


def test_release_builder_carries_runtime_source_and_supports_safe_output_root():
    text = BUILDER.read_text(encoding="utf-8")
    for marker in (
        'OutputRoot = ""',
        "$artifactRoot",
        '"src"',
        'BHM-Release-$Version.zip',
        '$canonicalExe = Join-Path $repoRoot $exeName',
        'Copy-Item -LiteralPath $compiledExe -Destination $canonicalExe -Force',
    ):
        assert marker in text


def test_release_verifier_requires_the_python_runtime_payload():
    text = VERIFIER.read_text(encoding="utf-8")
    for marker in (
        '"src/blackholememory/app.py"',
        '"src/blackholememory/version_manifest.py"',
    ):
        assert marker in text


def test_postinstall_can_require_runtime_source_for_the_next_release():
    text = POSTINSTALL.read_text(encoding="utf-8")
    assert "RequireRuntimeSource" in text
    assert "src/blackholememory/app.py" in text
