"""Late-bound handle for code split from ``bhm_crystallize_worker.py``.

The worker may run as ``__main__`` or be imported as ``bhm_crystallize_worker``; split modules read the worker's globals as
``_worker.NAME`` at call time so ``monkeypatch.setattr(bhm_crystallize_worker, "NAME", ...)`` keeps working. When a split
module is imported before the worker, the first lookup imports the worker, which binds itself.
"""

from __future__ import annotations

import importlib
from types import ModuleType


class _WorkerHandle:
    _module: ModuleType | None = None

    def bind(self, module: ModuleType) -> None:
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name: str):
        module = object.__getattribute__(self, "_module")
        if module is None:
            importlib.import_module("bhm_crystallize_worker")  # binds itself on import
            module = object.__getattribute__(self, "_module")
        if module is None:
            raise AttributeError(f"bhm_crystallize_worker is not bound (looking up {name})")
        return getattr(module, name)


worker = _WorkerHandle()


def bind(module: ModuleType) -> None:
    worker.bind(module)
