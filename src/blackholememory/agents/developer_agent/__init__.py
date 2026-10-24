# ruff: noqa

"""Compatibility facade for the developer agent package."""

import sys
import types

from . import _common as _common
from . import _core as _core
from . import executor_shared as executor_shared
from . import executor_shared_base as executor_shared_base, executor_shared_censor as executor_shared_censor, executor_shared_nodes as executor_shared_nodes
from . import llm_client as llm_client
from . import quarantine as quarantine
from . import swarm_tools as swarm_tools
from ._common import *  # noqa: F401,F403
from ._core import *  # noqa: F401,F403
from .llm_client import *  # noqa: F401,F403
from .quarantine import *  # noqa: F401,F403
from .swarm_tools import *  # noqa: F401,F403

# Cross-module tool implementations are intentionally bound after import to
# avoid cycles while preserving the original module-level call surface.
swarm_tools.analyze_screenshot = analyze_screenshot
swarm_tools.tool_get_file_outline = tool_get_file_outline
swarm_tools.tool_get_symbol_definition = tool_get_symbol_definition
swarm_tools.DeveloperAgentState = _core.DeveloperAgentState


def _terminate_spawned_pid_tree(pid: int, grace_seconds: int = PROCESS_EXECUTION_TERMINATION_GRACE_SECONDS):
    """Delegate through facade-patched helpers for backwards compatibility."""

    swarm_tools._is_pid_running = globals()["_is_pid_running"]
    swarm_tools._grace_wait = globals()["_grace_wait"]
    swarm_tools._bounded_termination_grace_seconds = globals()["_bounded_termination_grace_seconds"]
    return swarm_tools._terminate_spawned_pid_tree(pid, grace_seconds)


class _DeveloperAgentFacadeModule(types.ModuleType):
    """Keep legacy facade monkeypatches bound to their owning split module."""

    _PATCH_TARGETS = {
        "_MCP_PLAYWRIGHT_SEARCH_PROVIDER": (_common, executor_shared, executor_shared_base, executor_shared_censor, executor_shared_nodes),
        "_SPECULATIVE_RAG_SEARCH_PROVIDER": (_common, executor_shared, executor_shared_base, executor_shared_censor, executor_shared_nodes),
        "_read_bhm_env": (_common, _core, llm_client, executor_shared, executor_shared_base, executor_shared_censor, executor_shared_nodes),
        "configured_caller_token": (_common,),
        "analyze_screenshot": (swarm_tools,),
        "_repo_root": (_common, quarantine),
        "_is_pid_running": (swarm_tools, _core, executor_shared),
        "_grace_wait": (swarm_tools,),
        "_terminate_spawned_pid_tree": (executor_shared, _core),
    }

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        targets = self._PATCH_TARGETS.get(name)
        if not targets:
            return
        for module in targets:
            setattr(module, name, value)


sys.modules[__name__].__class__ = _DeveloperAgentFacadeModule


__all__ = [name for name in globals() if not name.startswith("__")]
