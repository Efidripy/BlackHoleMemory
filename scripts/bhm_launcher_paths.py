"""Launcher filesystem and environment helpers."""

from __future__ import annotations

from bhm_launcher_base import CODEX_PLUGIN_ID

from bhm_launcher_host import launcher as _launcher


def is_frozen() -> bool:
    return bool(getattr(_launcher.sys, "frozen", False))


def bundled_resource_root() -> _launcher.Path | None:
    raw = getattr(_launcher.sys, "_MEIPASS", None)
    return _launcher.Path(raw).resolve() if raw else None


def cleanup_stale_pyinstaller_dirs(
    temp_root: _launcher.Path,
    current: _launcher.Path | None,
    *,
    min_age_seconds: float = 3600,
) -> list[_launcher.Path]:
    now = _launcher.time.time()
    current_path = current.resolve() if current is not None else None
    removed: list[_launcher.Path] = []
    for candidate in temp_root.glob("_MEI*"):
        try:
            if not candidate.is_dir() or candidate.is_symlink():
                continue
            is_junction = getattr(candidate, "is_junction", None)
            if is_junction is not None and is_junction():
                continue
            metadata = candidate.lstat()
            reparse_flag = getattr(_launcher.stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            if reparse_flag and int(getattr(metadata, "st_file_attributes", 0)) & reparse_flag:
                continue
            if current_path is not None and candidate.resolve() == current_path:
                continue
            if now - metadata.st_mtime < min_age_seconds:
                continue
            if not (
                (candidate / "assets" / "bhm-control-panel.ico").is_file()
                and (candidate / "scripts" / "bhm_launcher.py").is_file()
            ):
                continue
            renamed = temp_root / f"{candidate.name}.bhm-delete-{_launcher.os.getpid()}"
            _launcher.os.rename(candidate, renamed)
            _launcher.shutil.rmtree(renamed, ignore_errors=True)
            removed.append(candidate)
        except OSError:
            continue
    return removed


def app_dir() -> _launcher.Path:
    if _launcher.is_frozen():
        return _launcher.Path(_launcher.sys.executable).resolve().parent
    return _launcher.Path(__file__).resolve().parent


def candidate_roots() -> list[_launcher.Path]:
    base = _launcher.app_dir()
    roots = [base]
    if base.name.lower() in {"scripts", "dist"}:
        roots.append(base.parent)
    return list(dict.fromkeys(roots))


def find_project_root() -> _launcher.Path:
    for root in _launcher.candidate_roots():
        if (root / "pyproject.toml").exists() and (root / "src" / "blackholememory").exists():
            return root
        if (root / "scripts" / "run-service.ps1").exists():
            return root
    return _launcher.app_dir().parent if _launcher.app_dir().name.lower() in {"scripts", "dist"} else _launcher.app_dir()


def find_resource_root() -> _launcher.Path:
    bundled = _launcher.bundled_resource_root()
    if bundled:
        return bundled
    return _launcher.find_project_root()


def find_state_root() -> _launcher.Path:
    if _launcher.bundled_resource_root():
        project_root = _launcher.find_project_root()
        if (project_root / "pyproject.toml").is_file() and (project_root / "src" / "blackholememory").is_dir():
            return project_root
        base = _launcher.os.environ.get("LOCALAPPDATA") or str(_launcher.Path.home())
        return _launcher.Path(base) / "BlackHoleMemory"
    return _launcher.find_project_root()


def find_resource_cache_root() -> _launcher.Path:
    """Return the writable cache root for resources extracted from a bundle."""

    if _launcher.bundled_resource_root():
        base = _launcher.os.environ.get("LOCALAPPDATA")
        return (_launcher.Path(base) if base else _launcher.Path.home()) / "BlackHoleMemory"
    return _launcher.find_project_root()


def load_ui_version() -> str:
    """Read the UI display version from the canonical release manifest."""

    candidates = [_launcher.RESOURCE_ROOT / "config" / "version-manifest.json", _launcher.find_project_root() / "config" / "version-manifest.json"]
    for path in candidates:
        try:
            payload = _launcher.json.loads(path.read_text(encoding="utf-8"))
            value = str((payload.get("components") or {}).get("ui") or "").strip()
            if value:
                return value
        except (OSError, _launcher.json.JSONDecodeError, AttributeError):
            continue
    return "Runtime v2.0.2-PURE"


def has_docker() -> bool:
    return _launcher.shutil.which("docker") is not None


def host_python_executable() -> str:
    if not _launcher.is_frozen():
        return _launcher.sys.executable
    candidates = [
        _launcher.os.environ.get("PYTHON"),
        _launcher.shutil.which("python"),
        _launcher.shutil.which("py"),
        _launcher.shutil.which("python3"),
    ]
    for candidate in candidates:
        if candidate:
            return str(candidate)
    raise RuntimeError("Python was not found on PATH. Install Python 3.12+ or set PYTHON to python.exe.")


def ensure_persistent_file(relative_path: str) -> _launcher.Path:
    source = _launcher.RESOURCE_ROOT / relative_path
    if not source.exists():
        fallback = _launcher.find_project_root() / relative_path
        if fallback.exists():
            source = fallback
    if not source.exists():
        raise FileNotFoundError(source)
    if not _launcher.bundled_resource_root():
        return source
    destination = _launcher.PERSISTENT_RESOURCE_ROOT / relative_path
    _launcher._assert_owned_path(destination, _launcher.PERSISTENT_RESOURCE_ROOT, require_directory=False)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _launcher._assert_owned_path(destination, _launcher.PERSISTENT_RESOURCE_ROOT, require_directory=False)
    _launcher.shutil.copy2(source, destination)
    return destination


def _assert_owned_path(
    destination: _launcher.Path,
    owner_root: _launcher.Path,
    *,
    require_directory: bool,
) -> None:
    """Fail closed before mutating a path under its owner root.

    The launcher may remove and recreate these directories during setup.  A
    path that escaped the owner root, points through a symlink/junction, or is
    replaced with an unexpected file/hardlink must never be handed to a
    destructive copy/remove operation.
    ``lstat`` is used deliberately so Windows reparse-point provenance is not
    lost by following the path first.
    """

    owner = _launcher.Path(_launcher.os.path.abspath(_launcher.os.fspath(owner_root)))
    target = _launcher.Path(_launcher.os.path.abspath(_launcher.os.fspath(destination)))
    try:
        relative_parts = target.relative_to(owner).parts
    except ValueError as exc:
        raise RuntimeError(
            f"Refusing to mutate plugin target outside owner root: {target}"
        ) from exc

    paths = [owner]
    for part in relative_parts:
        paths.append(paths[-1] / part)

    for current in paths:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise RuntimeError(f"Unable to inspect plugin target: {current}") from exc

        attributes = int(getattr(metadata, "st_file_attributes", 0))
        if current.is_symlink() or attributes & 0x400:
            raise RuntimeError(
                f"Refusing to mutate owned path through symlink/junction/reparse path: {current}"
            )
        if not _launcher.stat.S_ISDIR(metadata.st_mode):
            if current == target:
                if require_directory:
                    raise RuntimeError(f"Owned target is not a directory: {current}")
                if getattr(metadata, "st_nlink", 1) > 1:
                    raise RuntimeError(f"Owned target is a hardlink: {current}")
                continue
            raise RuntimeError(f"Owned path component is not a directory: {current}")


def _assert_owned_plugin_target(destination: _launcher.Path, owner_root: _launcher.Path) -> None:
    """Fail closed before mutating a plugin directory under its owner root."""

    _launcher._assert_owned_path(destination, owner_root, require_directory=True)


def _assert_launchable_source(source: _launcher.Path, owner_root: _launcher.Path) -> _launcher.Path:
    """Reject redirected or non-regular process sources before execution.

    Launcher service operations execute checked-in PowerShell/Compose sources.
    Validate the source while preserving ``lstat`` provenance so a linked or
    hardlinked replacement cannot be handed to a child process after the
    existence check.
    """

    target = _launcher.Path(source)
    if not target.is_file():
        raise FileNotFoundError(target)
    _launcher._assert_owned_path(target, owner_root, require_directory=False)
    return target


def ensure_persistent_plugin_source() -> _launcher.Path:
    source = _launcher.RESOURCE_ROOT / "plugins" / CODEX_PLUGIN_ID
    if not (source / ".codex-plugin" / "plugin.json").exists():
        fallback = _launcher.find_project_root() / "plugins" / CODEX_PLUGIN_ID
        if (fallback / ".codex-plugin" / "plugin.json").exists():
            source = fallback
    if not (source / ".codex-plugin" / "plugin.json").exists():
        raise FileNotFoundError(f"BHM Codex plugin bundle was not found at {source}")
    if not _launcher.bundled_resource_root():
        return source.resolve()
    destination = _launcher.PERSISTENT_RESOURCE_ROOT / "plugins" / CODEX_PLUGIN_ID
    _launcher._assert_owned_plugin_target(destination, _launcher.PERSISTENT_RESOURCE_ROOT)
    if destination.exists():
        _launcher.shutil.rmtree(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    _launcher._assert_owned_plugin_target(destination, _launcher.PERSISTENT_RESOURCE_ROOT)
    _launcher.shutil.copytree(source, destination)
    return destination.resolve()


def environment_ready() -> bool:
    if _launcher.force_setup_requested():
        return False
    return _launcher.has_virtualenv() and _launcher.has_docker()


def force_setup_requested() -> bool:
    env_value = _launcher.os.environ.get("BHM_FORCE_SETUP", "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        return True
    if "--force-setup" in _launcher.sys.argv:
        return True
    return "setuptest" in _launcher.Path(_launcher.sys.executable).stem.lower()


def now_text() -> str:
    return _launcher.datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def hidden_startupinfo() -> _launcher.subprocess.STARTUPINFO | None:
    if _launcher.os.name != "nt":
        return None
    startupinfo = _launcher.subprocess.STARTUPINFO()
    startupinfo.dwFlags |= _launcher.subprocess.STARTF_USESHOWWINDOW
    startupinfo.wShowWindow = _launcher.subprocess.SW_HIDE
    return startupinfo


def creation_flags() -> int:
    return (_launcher.CREATE_NO_WINDOW | _launcher.CREATE_NEW_PROCESS_GROUP) if _launcher.os.name == "nt" else 0
