"""Late-bound handle on the launcher module for code split out of ``bhm_launcher.py``.

The launcher may run as ``__main__`` or be imported as ``bhm_launcher``; split modules cannot import it by name without
loading a second copy. ``bhm_launcher`` calls ``bind(sys.modules[__name__])`` once its Qt names exist, and split modules
read launcher globals as ``launcher.NAME`` at call time, so ``monkeypatch.setattr(bhm_launcher, "NAME", ...)`` keeps working.
"""

from __future__ import annotations

from types import ModuleType


class _LauncherHandle:
    _module: ModuleType | None = None

    def bind(self, module: ModuleType) -> None:
        object.__setattr__(self, "_module", module)

    def __getattr__(self, name: str):
        module = object.__getattribute__(self, "_module")
        if module is None:
            raise AttributeError(f"launcher module not bound yet (looking up {name})")
        return getattr(module, name)


launcher = _LauncherHandle()


def bind(module: ModuleType) -> None:
    launcher.bind(module)
