from __future__ import annotations

import sys
from types import ModuleType, SimpleNamespace

from blackholememory.agents import developer_agent


class _ImageNotFound(Exception):
    pass


class _DockerException(Exception):
    pass


class _ContainerError(Exception):
    pass


class _FakeContainer:
    def __init__(self) -> None:
        self.started = False
        self.killed = False
        self.removed = False

    def start(self) -> None:
        self.started = True

    def wait(self, timeout: int) -> dict[str, int]:
        assert timeout > 0
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool) -> bytes:
        return b"ok\n" if stdout and not stderr else b""

    def kill(self) -> None:
        self.killed = True

    def remove(self) -> None:
        self.removed = True


def _install_fake_docker(monkeypatch, *, image_missing: bool = False):
    calls: dict[str, object] = {"get": [], "pull": [], "create": []}
    container = _FakeContainer()

    class Images:
        def get(self, name: str) -> object:
            calls["get"].append(name)
            if image_missing:
                raise _ImageNotFound(name)
            return object()

        def pull(self, name: str) -> object:
            calls["pull"].append(name)
            raise AssertionError("runtime image pull must never occur")

    class Containers:
        def create(self, **kwargs):
            calls["create"].append(kwargs)
            return container

    client = SimpleNamespace(images=Images(), containers=Containers())
    module = ModuleType("docker")
    module.errors = SimpleNamespace(
        DockerException=_DockerException,
        ImageNotFound=_ImageNotFound,
        ContainerError=_ContainerError,
    )
    module.DockerClient = lambda **_kwargs: client
    module.from_env = lambda: client
    monkeypatch.setitem(sys.modules, "docker", module)
    return calls, container


def test_sandbox_uses_pinned_image_and_hardened_container(monkeypatch) -> None:
    calls, container = _install_fake_docker(monkeypatch)
    monkeypatch.setattr(developer_agent.platform, "system", lambda: "Windows")

    result = developer_agent.sandbox_exec("print('ok')")

    assert result["success"] is True
    assert calls["get"] == [developer_agent.SANDBOX_IMAGE]
    assert calls["pull"] == []
    assert len(calls["create"]) == 1
    kwargs = calls["create"][0]
    assert kwargs["image"] == developer_agent.SANDBOX_IMAGE
    assert kwargs["network_mode"] == "none"
    assert kwargs["network_disabled"] is True
    assert kwargs["read_only"] is True
    assert kwargs["user"] == "65534:65534"
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges:true"]
    assert kwargs["pids_limit"] == 64
    assert kwargs["mem_limit"] == "50m"
    assert kwargs["memswap_limit"] == "50m"
    assert kwargs["privileged"] is False
    assert "nodev" in kwargs["tmpfs"]["/tmp"]
    assert "ports" not in kwargs
    assert "volumes" not in kwargs
    assert "devices" not in kwargs
    assert container.started is True
    assert container.removed is True


def test_missing_pinned_image_fails_closed_without_runtime_pull(monkeypatch) -> None:
    calls, _container = _install_fake_docker(monkeypatch, image_missing=True)
    monkeypatch.setattr(developer_agent.platform, "system", lambda: "Windows")

    result = developer_agent.sandbox_exec("print('never runs')")

    assert result["success"] is False
    assert "Pinned sandbox image is not installed" in result["stderr"]
    assert calls["pull"] == []
    assert calls["create"] == []
