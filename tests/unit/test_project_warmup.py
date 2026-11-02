from __future__ import annotations

import asyncio
import logging

import pytest

from blackholememory import app_core_fallback, project_warmup


def _setup(monkeypatch, projects):
    monkeypatch.delenv("BHM_PROJECT_WARMUP", raising=False)
    monkeypatch.setattr(project_warmup, "resolve_retrieval_projects", lambda requested, service: projects)
    monkeypatch.setattr(project_warmup, "_PAUSE_SECONDS", 0)


def test_sequential_warmup_waits_for_provider_and_skips_failure(monkeypatch, caplog):
    _setup(monkeypatch, ["one", "bad", "three"])
    events = []

    async def run(worker, project):
        events.append(project)
        if project == "bad":
            raise ValueError("failure")

    monkeypatch.setattr(app_core_fallback, "_run_bounded_provider_call", run)

    async def exercise():
        gate = asyncio.Event()
        provider = asyncio.create_task(gate.wait())
        task = asyncio.create_task(project_warmup.warmup_projects(provider))
        await asyncio.sleep(0)
        assert not events
        gate.set()
        await task

    with caplog.at_level(logging.ERROR):
        asyncio.run(exercise())
    assert events == ["one", "bad", "three"]
    assert "Project provider warmup failed for bad" in caplog.text


def test_disabled_does_not_wait_or_list_projects(monkeypatch):
    monkeypatch.setenv("BHM_PROJECT_WARMUP", "0")

    async def exercise():
        provider = asyncio.create_task(asyncio.Event().wait())
        await project_warmup.warmup_projects(provider)
        assert not provider.done()
        provider.cancel()
        await asyncio.gather(provider, return_exceptions=True)

    asyncio.run(exercise())


def test_cancellation_does_not_cancel_running_provider_call(monkeypatch):
    _setup(monkeypatch, ["one", "two"])
    events = []

    async def exercise():
        started = asyncio.Event()
        release = asyncio.Event()
        finished = asyncio.Event()

        async def run(worker, project):
            events.append(project)
            started.set()
            await release.wait()
            finished.set()

        monkeypatch.setattr(app_core_fallback, "_run_bounded_provider_call", run)
        provider = asyncio.create_task(asyncio.sleep(0))
        task = asyncio.create_task(project_warmup.warmup_projects(provider))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        release.set()
        await finished.wait()

    asyncio.run(exercise())
    assert events == ["one"]


def test_capacity_retries_same_project(monkeypatch):
    _setup(monkeypatch, ["one", "two"])
    events = []

    async def run(worker, project):
        events.append(project)
        if len(events) == 1:
            raise app_core_fallback.RetrievalProviderCapacityExceeded("busy")

    monkeypatch.setattr(app_core_fallback, "_run_bounded_provider_call", run)

    async def exercise():
        provider = asyncio.create_task(asyncio.sleep(0))
        await project_warmup.warmup_projects(provider)

    asyncio.run(exercise())
    assert events == ["one", "one", "two"]
