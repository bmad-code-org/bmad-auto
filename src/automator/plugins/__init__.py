"""bmad-auto plugin system.

A first-class, general extension layer for the orchestrator: a plugin extends
the automator without modifying core, ranging from a data-only settings
contribution to an in-process Python module. Distribution is folder-drop today
(builtins under ``automator/data/plugins/``, project-local under
``.automator/plugins/``) with a locked seam for entry-point packaging later.

Phase 0 ships the foundation — manifest model, loader, trust gate, registry —
wired into nothing yet. The hook bus, dynamic settings, and the engine migration
build on this in later phases.
"""

from __future__ import annotations

from .loader import (
    ENTRY_POINT_GROUP,
    USER_PLUGINS_REL,
    discover,
    get_plugin,
    load_plugins,
)
from .model import (
    API_VERSION,
    SETTING_TYPES,
    SUPPORTED_API,
    HookSpec,
    LoadedPlugin,
    Plugin,
    PluginError,
    PluginManifest,
    PythonSpec,
    SettingSpec,
)
from .registry import PluginRegistry
from .trust import PluginUntrusted, is_enabled, require_enabled

__all__ = [
    "API_VERSION",
    "SUPPORTED_API",
    "SETTING_TYPES",
    "ENTRY_POINT_GROUP",
    "USER_PLUGINS_REL",
    "HookSpec",
    "SettingSpec",
    "PythonSpec",
    "PluginManifest",
    "Plugin",
    "LoadedPlugin",
    "PluginError",
    "PluginUntrusted",
    "PluginRegistry",
    "discover",
    "load_plugins",
    "get_plugin",
    "is_enabled",
    "require_enabled",
]
