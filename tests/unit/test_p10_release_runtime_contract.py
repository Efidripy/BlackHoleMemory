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
            'Copy-Item -LiteralPath $releaseExe -Destination $canonicalExe -Force',
    ):
        assert marker in text


def test_release_builder_initializes_cleanup_state_before_preflight_failures():
    text = BUILDER.read_text(encoding="utf-8")
    assert "$releaseEnv = $null" in text
    assert "IsNullOrWhiteSpace([string]$releaseEnv)" in text
    assert "$failure = $_" in text
    assert "throw $failure.Exception" in text


def test_release_builder_quotes_git_tree_revision_for_powershell() -> None:
    text = BUILDER.read_text(encoding="utf-8")
    assert "rev-parse 'HEAD^{tree}'" in text
    assert "rev-parse HEAD^{tree}" not in text


def test_release_builder_bootstraps_source_package_for_seed_python() -> None:
    text = BUILDER.read_text(encoding="utf-8")
    assert '$sourcePackageRoot = Join-Path $repoRoot "src"' in text
    assert '$env:PYTHONPATH = if ([string]::IsNullOrWhiteSpace($previousPythonPath))' in text
    assert "PathSeparator" in text
    assert "Remove-Item Env:PYTHONPATH" in text


def test_release_builder_defers_canonical_launcher_mutation_until_success():
    text = BUILDER.read_text(encoding="utf-8")
    assert "if ($RefreshCanonicalLauncher)" in text
    assert "Write-Step \"Refreshing canonical desktop launcher\"" in text
    assert "foreach ($disposablePath in @($releaseRoot, $buildDir, $distDir, $zipPath, $releaseHashPath))" in text
    assert "uv sync --locked --no-install-project --extra build --project $repoRoot" in text
    assert '$distDir = Join-Path $releaseRoot "dist"' in text
    assert '$buildDir = Join-Path $releaseRoot "build"' in text
    assert "Assert-DisposablePathSafe" in text
    assert "reparse disposable" in text


def test_release_builder_uses_immutable_source_snapshot_for_compiler_and_copy():
    text = BUILDER.read_text(encoding="utf-8")
    assert "materialize-release-source.py" in text
    assert "Materializing immutable tracked source snapshot" in text
    assert "$sourceSnapshotRoot" in text
    assert 'Join-Path $sourceRoot "scripts\\bhm_launcher.py"' in text
    assert 'Join-Path $sourceRoot "assets\\bhm-control-panel.ico"' in text
    assert 'Join-Path $sourceRoot "pyproject.toml"' in text
    assert "$sourceSnapshot -ne [string]$sourceSnapshotResult.source_snapshot_sha256" in text


def test_release_builder_checks_custom_output_root_before_creation():
    text = BUILDER.read_text(encoding="utf-8")
    assert "Resolve-SafeArtifactRoot" in text
    assert "Custom OutputRoot must remain outside the repository checkout" in text
    assert "Refusing reparse-point artifact root component" in text


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
