"""Launcher logging, process, and HTTP transport helpers."""

from __future__ import annotations

from bhm_launcher_config import load_settings as load_validated_launcher_settings
from bhm_launcher_config import save_settings as save_validated_launcher_settings
from bhm_launcher_readiness import LocalEndpointError
from bhm_launcher_readiness import MAX_HTTP_BYTES
from bhm_launcher_telemetry import _TELEMETRY_REQUEST_STATE
from blackholememory.filesystem_boundaries import append_bytes_safely
from blackholememory.resource_limits import LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS
from blackholememory.resource_limits import PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS
from bhm_launcher_host import launcher as _launcher


def _rotate_launcher_log(path: _launcher.Path) -> None:
    stale = path.with_name(f"{path.name}.{_launcher.LAUNCHER_LOG_BACKUP_COUNT + 1}")
    if stale.exists():
        stale.unlink()
    for index in range(_launcher.LAUNCHER_LOG_BACKUP_COUNT - 1, 0, -1):
        source = path.with_name(f"{path.name}.{index}")
        destination = path.with_name(f"{path.name}.{index + 1}")
        if source.exists():
            source.replace(destination)
    if path.exists():
        path.replace(path.with_name(f"{path.name}.1"))


def append_launcher_log(line: str) -> None:
    try:
        _launcher.LAUNCHER_LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = _launcher.LAUNCHER_LOG_DIR / "unified-launcher.log"
        payload = f"[{_launcher.now_text()}] {line}\n".encode("utf-8")
        with _launcher._LAUNCHER_LOG_LOCK:
            try:
                current_size = path.stat().st_size if path.exists() else 0
                if current_size + len(payload) > _launcher.LAUNCHER_LOG_MAX_BYTES:
                    try:
                        _launcher._rotate_launcher_log(path)
                    except OSError:
                        pass
            except OSError:
                pass
            append_bytes_safely(path, payload)
    except OSError:
        pass


def load_launcher_settings() -> dict:
    result = load_validated_launcher_settings(
        _launcher.LAUNCHER_SETTINGS_PATH,
        backup_dir=_launcher.LAUNCHER_SETTINGS_BACKUP_DIR,
    )
    if not result.ok:
        _launcher.append_launcher_log(
            f"SETTINGS INVALID: {result.error}; preserved={_launcher.LAUNCHER_SETTINGS_PATH}; "
            f"backup={result.backup_path or 'unavailable'}"
        )
    return result.settings


def save_launcher_settings(settings: dict) -> None:
    result = save_validated_launcher_settings(
        _launcher.LAUNCHER_SETTINGS_PATH,
        settings,
        backup_dir=_launcher.LAUNCHER_SETTINGS_BACKUP_DIR,
    )
    _launcher.append_launcher_log(
        f"SETTINGS SAVED: backup={result.backup_path or 'none'}; path={_launcher.LAUNCHER_SETTINGS_PATH}"
    )


def http_status(url: str, timeout: float = LAUNCHER_HTTP_PROBE_TIMEOUT_SECONDS) -> _launcher.ServiceStatus:
    ok, detail = _launcher.probe_http(url, timeout=timeout)
    if ok:
        return _launcher.ServiceStatus("Running", detail)
    if detail.startswith("HTTP "):
        return _launcher.ServiceStatus("Error", detail)
    return _launcher.ServiceStatus("Stopped", detail)


def stabilize_service_status(
    previous: _launcher.ServiceStatus | None,
    observed: _launcher.ServiceStatus,
    consecutive_failures: int,
    *,
    failure_threshold: int = _launcher.SERVICE_FAILURE_THRESHOLD,
) -> tuple[_launcher.ServiceStatus, int]:
    """Debounce transient local probe timeouts before declaring a service down."""

    if observed.state == "Running":
        return observed, 0
    failures = consecutive_failures + 1
    if previous is not None and previous.state in {"Running", "Recovering"} and failures < failure_threshold:
        return _launcher.ServiceStatus("Recovering", f"transient probe failure {failures}/{failure_threshold}"), failures
    return observed, failures


def run_bounded_control_command(
    args: list[str],
    *,
    cwd: _launcher.Path | None = None,
) -> _launcher.subprocess.CompletedProcess | None:
    """Run an operator process-control command without an unbounded wait."""

    try:
        return _launcher.subprocess.run(
            args,
            cwd=str(cwd) if cwd is not None else None,
            stdin=_launcher.subprocess.DEVNULL,
            stdout=_launcher.subprocess.DEVNULL,
            stderr=_launcher.subprocess.DEVNULL,
            check=False,
            timeout=_launcher.PROCESS_CONTROL_TIMEOUT_SECONDS,
            creationflags=_launcher.creation_flags(),
            startupinfo=_launcher.hidden_startupinfo(),
        )
    except (OSError, _launcher.subprocess.TimeoutExpired) as exc:
        _launcher.append_launcher_log(
            "CONTROL COMMAND FAILED: "
            + " ".join(args)
            + f"; error={_launcher.compact_error(exc)}"
        )
        return None


def terminate_process_tree(process: _launcher.subprocess.Popen | None) -> None:
    """Stop only the process started by this launcher operation."""

    if process is None or process.poll() is not None:
        return
    if _launcher.os.name == "nt":
        _launcher.run_bounded_control_command(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
        )
        return
    try:
        process.terminate()
        process.wait(timeout=PROCESS_EXECUTION_SHUTDOWN_TIMEOUT_SECONDS)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _read_process_or_user_env_value(key: str) -> str | None:
    if key.startswith("BHM_CALLER_") and _launcher.os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
                value, _ = winreg.QueryValueEx(handle, key)
            user_value = str(value or "").strip()
            if user_value:
                return user_value
        except (ImportError, FileNotFoundError, OSError):
            pass
    direct = str(_launcher.os.getenv(key) or "").strip()
    if direct:
        return direct
    if _launcher.os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as handle:
            value, _ = winreg.QueryValueEx(handle, key)
    except (ImportError, FileNotFoundError, OSError):
        return None
    return str(value or "").strip() or None


def _required_bhm_caller_token() -> str:
    token = _launcher._read_process_or_user_env_value("BHM_CALLER_TOKEN") or ""
    if len(token) < 32:
        raise RuntimeError("BHM caller credential is unavailable; initialize BHM_CALLER_TOKEN")
    return token


def validate_launcher_project(value: str) -> str:
    project = str(value or "").strip()
    if not _launcher.re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", project):
        raise ValueError("project must be a simple project id")
    return project


def resolve_launcher_project(settings: dict[str, _launcher.Any] | None = None) -> str:
    configured = (settings or {}).get("project")
    candidates = [
        configured,
        _launcher._read_process_or_user_env_value("BHM_CALLER_DEFAULT_PROJECT"),
    ]
    allowed = _launcher._read_process_or_user_env_value("BHM_CALLER_PROJECTS") or ""
    allowed_projects = [item.strip() for item in allowed.split(",") if item.strip() and item.strip() != "*"]
    if len(allowed_projects) == 1:
        candidates.append(allowed_projects[0])
    candidates.append(_launcher.DEFAULT_LAUNCHER_PROJECT)
    for candidate in candidates:
        try:
            return _launcher.validate_launcher_project(str(candidate or ""))
        except ValueError:
            continue
    return _launcher.DEFAULT_LAUNCHER_PROJECT


def _caller_headers(*, project: str | None = None, admin: bool = False) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {_launcher._required_bhm_caller_token()}",
        "Content-Type": "application/json",
        "User-Agent": "BHM-Control-Deck",
        "X-BHM-Caller-Surface": "launcher",
    }
    if project:
        headers["X-BHM-Caller-Project"] = _launcher.validate_launcher_project(project)
    if admin:
        admin_capability = _launcher._read_process_or_user_env_value("BHM_ADMIN_CAPABILITY") or _launcher._read_process_or_user_env_value(
            "BHM_MCP_ADMIN_CAPABILITY"
        )
        if admin_capability:
            headers["X-BHM-Admin-Capability"] = admin_capability
    return headers


def request_json(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, _launcher.Any] | None = None,
    project: str | None = None,
    timeout: float = _launcher.TELEMETRY_TIMEOUT,
    max_bytes: int = MAX_HTTP_BYTES,
    admin: bool = False,
) -> dict[str, _launcher.Any]:
    normalized_method = method.upper()
    data = None
    if normalized_method != "GET":
        data = _launcher.json.dumps(payload if payload is not None else {"project": project}).encode("utf-8")
    request = _launcher.urllib.request.Request(
        url,
        data=data,
        headers=_launcher._caller_headers(project=project, admin=admin),
        method=normalized_method,
    )
    # The endpoint policy validates the configured origin separately from a
    # target that carries query parameters.  Query-free calls keep the simpler
    # validation path used by existing launcher probes.
    open_kwargs: dict[str, _launcher.Any] = {"timeout": timeout}
    if _launcher.urlsplit(url).query:
        open_kwargs["endpoint"] = _launcher.BHM_BASE_URL
    with _launcher.open_local_url(request, **open_kwargs) as response:
        raw = _launcher.read_bounded_response(response, limit=max(1, min(int(max_bytes), 1024 * 1024)))
    decoded = _launcher.json.loads(raw.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("local JSON response must be an object")
    return decoded


def post_json(url: str, payload: dict | None = None, timeout: float = _launcher.TELEMETRY_TIMEOUT) -> dict:
    request_payload = payload if payload is not None else {"project": None}
    raw_project = request_payload.get("project") if isinstance(request_payload, dict) else None
    project = str(raw_project) if raw_project else None
    return _launcher.request_json(url, method="POST", payload=request_payload, project=project, timeout=timeout)


def get_json(url: str, *, project: str | None = None, timeout: float = _launcher.TELEMETRY_TIMEOUT) -> dict[str, _launcher.Any]:
    return _launcher.request_json(url, method="GET", project=project, timeout=timeout)


def _is_bhm_human_ui_url(url: str) -> bool:
    """Return whether a launcher link requires the short-lived browser bootstrap."""

    try:
        candidate = _launcher.urlsplit(url)
        base = _launcher.urlsplit(_launcher.BHM_BASE_URL)
    except ValueError:
        return False
    if candidate.scheme.casefold() != base.scheme.casefold():
        return False
    if candidate.netloc.casefold() != base.netloc.casefold():
        return False
    normalized_path = candidate.path.rstrip("/") or "/"
    return normalized_path in _launcher.BHM_HUMAN_UI_PATHS


def mint_bhm_ui_bootstrap_token(*, timeout: float = _launcher.UI_SESSION_MINT_TIMEOUT_SECONDS) -> str:
    """Mint one single-use UI bootstrap without exposing the caller credential."""

    try:
        payload = _launcher.post_json(
            f"{_launcher.BHM_BASE_URL}/bhm/ui/session/mint",
            {"project": None},
            timeout=timeout,
        )
    except Exception as exc:
        raise _launcher.LauncherUiSessionError("BHM UI session could not be established") from exc
    token = payload.get("bootstrap_token") if isinstance(payload, dict) else None
    if not isinstance(token, str) or token != token.strip() or not 32 <= len(token) <= 256:
        raise _launcher.LauncherUiSessionError("BHM UI session response was invalid")
    return token


def browser_target_for_url(
    url: str,
    *,
    mint: _launcher.Callable[[], str] | None = None,
) -> str:
    """Prepare a browser target, minting only for BHM human UI surfaces."""

    if not _launcher._is_bhm_human_ui_url(url):
        return url
    bootstrap_token = (mint or _launcher.mint_bhm_ui_bootstrap_token)()
    parsed = _launcher.urlsplit(url)
    normalized_path = parsed.path.rstrip("/") or "/"
    if normalized_path in _launcher.BHM_GALAXY_VIEWER_PATHS:
        scoped_projects = _launcher._read_process_or_user_env_value("BHM_CALLER_PROJECTS") or ""
        default_project = _launcher._read_process_or_user_env_value("BHM_CALLER_DEFAULT_PROJECT") or ""
        if (
            scoped_projects
            and scoped_projects != "*"
            and default_project
            and "project" not in _launcher.parse_qs(parsed.query, keep_blank_values=True)
        ):
            query = f"{parsed.query}&project={_launcher.quote(default_project, safe='')}" if parsed.query else f"project={_launcher.quote(default_project, safe='')}"
            parsed = parsed._replace(query=query)
    bootstrap_fragment = f"{_launcher.BHM_UI_BOOTSTRAP_FRAGMENT_KEY}={_launcher.quote(bootstrap_token, safe='')}"
    fragment = f"{parsed.fragment}&{bootstrap_fragment}" if parsed.fragment else bootstrap_fragment
    return _launcher.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, fragment))


def open_launcher_link(
    url: str,
    *,
    opener: _launcher.Callable[[str], bool] | None = None,
    mint: _launcher.Callable[[], str] | None = None,
) -> None:
    """Open a launcher link and fail closed if secure UI bootstrap or browser launch fails."""

    target = _launcher.browser_target_for_url(url, mint=mint)
    if (opener or _launcher.webbrowser.open)(target) is False:
        raise _launcher.LauncherUiSessionError("Browser did not accept the launcher link")


def safe_post_json(url: str, payload: dict | None = None, timeout: float = _launcher.TELEMETRY_TIMEOUT) -> dict:
    try:
        return _launcher.post_json(url, payload, timeout)
    except Exception:
        return {}


def safe_json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, _launcher.Any] | None = None,
    project: str | None = None,
    timeout: float = _launcher.TELEMETRY_TIMEOUT,
    max_bytes: int = MAX_HTTP_BYTES,
    admin: bool = False,
) -> _launcher.JsonRequestResult:
    path = _launcher.urlsplit(url).path
    try:
        data = _launcher.request_json(url, method=method, payload=payload, project=project, timeout=timeout, max_bytes=max_bytes, admin=admin)
        result = _launcher.JsonRequestResult(ok=True, data=data, status_code=200)
        _TELEMETRY_REQUEST_STATE.record_result(path, result)
        return result
    except _launcher.urllib.error.HTTPError as exc:
        code = int(exc.code)
        label = "AUTH" if code in {401, 403} else f"HTTP {code}"
        result = _launcher.JsonRequestResult(ok=False, data={}, status_code=code, error=label)
        _TELEMETRY_REQUEST_STATE.record_result(path, result, error_class=exc.__class__.__name__)
        return result
    except (LocalEndpointError, OSError, ValueError, _launcher.json.JSONDecodeError, UnicodeDecodeError) as exc:
        label = "OFFLINE" if isinstance(exc, (OSError, _launcher.urllib.error.URLError)) else "INVALID"
        result = _launcher.JsonRequestResult(ok=False, data={}, error=label)
        _TELEMETRY_REQUEST_STATE.record_result(path, result, error_class=exc.__class__.__name__)
        return result


def _result_error_label(results: list[_launcher.JsonRequestResult]) -> str:
    if any(result.status_code in {401, 403} for result in results):
        return "AUTH"
    if any(result.error == "INVALID" for result in results):
        return "INVALID"
    return "OFFLINE"


def compact_error(exc: BaseException) -> str:
    text = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return text[:140] if text else exc.__class__.__name__
