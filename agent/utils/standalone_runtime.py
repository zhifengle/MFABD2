"""Helpers for running MaaFramework and custom extensions in one process.

The normal agent entry point is an AgentServer process.  The Unity bridge
entry point is different: it owns a MaaFramework Resource and Tasker itself.
Importing ``maa.agent`` normally switches the process-wide ``Library`` object
to AgentServer mode, so its decorators must be collected without calling the
AgentServer DLL.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from typing import Any

_ACTION_INSTANCES: dict[str, Any] | None = None
_RECOGNITION_INSTANCES: dict[str, Any] | None = None


def register_standalone_extensions(resource: Any) -> tuple[list[str], list[str]]:
    """Import decorated extensions and register their instances on *resource*.

    MaaFw's Python binding uses one process-wide library mode.  Calling
    ``Library.version()`` first finishes MaaFramework initialization, making
    the later ``maa.agent`` package initialization a no-op.  During extension
    imports we temporarily replace AgentServer's registration functions with
    local collectors, then register the collected instances on the Resource.
    """

    from maa.library import Library

    if Library.is_agent_server():
        raise RuntimeError(
            "Standalone extensions require MaaFramework mode, but the process "
            "is already in AgentServer mode"
        )

    # Library.open() ignores later mode changes after this initialization.
    Library.version()

    from maa.agent.agent_server import AgentServer

    if Library.is_agent_server():
        raise RuntimeError("maa.agent unexpectedly switched to AgentServer mode")

    global _ACTION_INSTANCES, _RECOGNITION_INSTANCES

    if _ACTION_INSTANCES is None or _RECOGNITION_INSTANCES is None:
        actions: dict[str, Any] = {}
        recognitions: dict[str, Any] = {}

        original_action: Callable[..., bool] = AgentServer.register_custom_action
        original_recognition: Callable[..., bool] = (
            AgentServer.register_custom_recognition
        )

        def collect_action(name: str, action: Any) -> bool:
            actions[name] = action
            return True

        def collect_recognition(name: str, recognition: Any) -> bool:
            recognitions[name] = recognition
            return True

        AgentServer.register_custom_action = staticmethod(collect_action)
        AgentServer.register_custom_recognition = staticmethod(collect_recognition)
        try:
            importlib.import_module("action")
            importlib.import_module("recognition")
            importlib.import_module("fishing_agent")
        finally:
            AgentServer.register_custom_action = staticmethod(original_action)
            AgentServer.register_custom_recognition = staticmethod(
                original_recognition
            )

        _ACTION_INSTANCES = actions
        _RECOGNITION_INSTANCES = recognitions

    for name, instance in _ACTION_INSTANCES.items():
        if not resource.register_custom_action(name, instance):
            raise RuntimeError(f"Failed to register custom action: {name}")
    for name, instance in _RECOGNITION_INSTANCES.items():
        if not resource.register_custom_recognition(name, instance):
            raise RuntimeError(f"Failed to register custom recognition: {name}")

    return list(_ACTION_INSTANCES), list(_RECOGNITION_INSTANCES)
