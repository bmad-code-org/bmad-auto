"""Data model for the bmad-auto plugin system.

A plugin extends the orchestrator without touching the core loop. Every plugin
is a ``plugin.toml`` manifest (metadata, declarative out-of-process hooks, a
settings schema, optional in-process Python). Data-only plugins need no code.

This module holds the immutable shapes the manifest parses into plus the
``Plugin`` base class that a trusted in-process plugin subclasses. It mirrors
the proven dataclass-as-data style of ``engines/plugin.py`` and
``adapters/profile.py`` — parsing/validation lives in ``manifest.py``,
discovery in ``loader.py``, trust in ``trust.py`` and aggregation in
``registry.py``. Nothing here reaches into the run loop; the hook bus that
calls into a plugin is wired in a later phase.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

# The framework's current plugin-API version. A manifest declares the api_version
# it was written against; the loader compares it against SUPPORTED_API. Bumping
# this set (never removing without a major) is how the API evolves.
API_VERSION = 1
SUPPORTED_API: frozenset[int] = frozenset({1})

# Permitted [[settings]] kinds. "select" carries an options list; the rest are
# scalars. Kept deliberately small — the settings pillar (Phase 1) maps these to
# widget hints.
SETTING_TYPES = {"bool", "int", "float", "str", "select"}

# Where a manifest was discovered, in overlay precedence order. Recorded so the
# loader can treat an api_version mismatch as a hard error for builtins but a
# skip-with-warning for third-party plugins.
PLUGIN_SOURCES = ("builtin", "entry_point", "project")


class PluginError(Exception):
    """Raised on a malformed manifest, an unknown plugin, or an unsupported
    builtin api_version. Mirrors EngineError/ProfileError."""


@dataclass(frozen=True)
class HookSpec:
    """A declarative, out-of-process hook bound to a lifecycle stage.

    ``stage`` is the ``[hooks.<stage>]`` table key. ``cmd`` is a shell command
    template that may use ``{scripts}`` (expanded to the plugin's script dir).
    A ``blocking`` hook's non-zero exit vetoes the unit (defer); a non-blocking
    hook is advisory (logged). Stage names are not validated here — the stage
    map is owned by the hook bus, wired in a later phase.
    """

    stage: str
    cmd: str = ""
    timeout_sec: int = 120
    blocking: bool = False
    # Blocking-hook failure policy. fail_open (default): a hook *error* (timeout,
    # missing interpreter) lets the run survive; only a clean non-zero exit
    # vetoes. fail_closed: any failure vetoes (defer). Consumed by the bus later.
    fail_closed: bool = False


@dataclass(frozen=True)
class SettingSpec:
    """One plugin-contributed setting, consumed by the settings pillar.

    The vocabulary intentionally matches the core settings ``_Field``: a key, a
    kind, a default, help text, and (for ``select``) an options list plus
    optional numeric bounds. Stored under ``[plugins.<name>]`` in policy.toml.
    """

    key: str
    type: str
    default: Any = None
    help: str = ""
    options: tuple[str, ...] = ()
    label: str = ""
    min: int | None = None
    max: int | None = None


@dataclass(frozen=True)
class PythonSpec:
    """Optional in-process module. Its presence makes the plugin trust-gated:
    the module is never imported unless the plugin is in ``[plugins] enabled``.

    ``module`` is a plugin-relative file (resolved against the plugin's script
    dir); ``cls`` is the ``Plugin`` subclass to instantiate (defaults to
    ``Plugin``).
    """

    module: str
    cls: str = "Plugin"


@dataclass(frozen=True)
class PluginManifest:
    """A parsed ``plugin.toml``. Immutable; the single inter-module contract.

    ``scripts_dir`` is the plugin's on-disk directory (its ``{scripts}`` root),
    set by the loader. ``source`` records discovery origin (see PLUGIN_SOURCES).
    """

    name: str
    version: str = "0.0.0"
    api_version: int = API_VERSION
    description: str = ""
    author: str = ""
    hooks: tuple[HookSpec, ...] = ()
    settings: tuple[SettingSpec, ...] = ()
    python: PythonSpec | None = None
    # custom orchestration items a plugin contributes (wired in a later phase).
    workflows: tuple[str, ...] = ()
    # extra gitignored paths to seed into a per-unit worktree, mirroring the
    # engine plugin (seed_files = literal project-relative paths, seed_globs =
    # patterns expanded against the main repo). Consumed by worktree priming.
    seed_files: tuple[str, ...] = ()
    seed_globs: tuple[str, ...] = ()
    # ordering across plugins at a shared stage: lower runs first, then load order.
    priority: int = 0
    scripts_dir: str = ""
    source: str = "project"

    def render(self, template: str) -> str:
        """Expand ``{scripts}`` in a command template to this plugin's dir."""
        return template.replace("{scripts}", self.scripts_dir)

    def hook_for(self, stage: str) -> HookSpec | None:
        for hook in self.hooks:
            if hook.stage == stage:
                return hook
        return None

    def setting_defaults(self) -> dict[str, Any]:
        return {s.key: s.default for s in self.settings}


class Plugin:
    """Base class for a trusted in-process plugin.

    A ``[python]`` manifest names a subclass; the registry instantiates it with
    the parsed manifest and the plugin's resolved settings (defaults overlaid by
    ``[plugins.<name>]`` in policy.toml). Hook-dispatch methods are added when
    the bus is wired in a later phase — for now the base simply carries context,
    so a folder-dropped plugin that is enabled can be constructed and inspected
    without yet being able to affect a run.

    Subclasses MUST NOT do expensive or side-effecting work in ``__init__``;
    construction happens at registry build time and a raised exception disables
    the instance (failure isolation).
    """

    def __init__(self, manifest: PluginManifest, settings: dict[str, Any]):
        self.manifest = manifest
        self.settings = settings

    @property
    def name(self) -> str:
        return self.manifest.name

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<Plugin {self.manifest.name!r} v{self.manifest.version}>"


@dataclass(frozen=True)
class LoadedPlugin:
    """A manifest plus its trust/resolution outcome, as the registry holds it.

    ``instance`` is a constructed ``Plugin`` for an enabled, trusted ``[python]``
    plugin; ``None`` for a data-only/declarative plugin, an untrusted one (has
    ``[python]`` but isn't allowlisted), or one whose construction raised.
    ``disabled`` marks an instance that errored and was isolated out.
    """

    manifest: PluginManifest
    instance: Plugin | None = None
    trusted: bool = True
    disabled: bool = False
    error: str = ""

    @property
    def name(self) -> str:
        return self.manifest.name


# re-exported by manifest.py for parser convenience
__all__ = [
    "API_VERSION",
    "SUPPORTED_API",
    "SETTING_TYPES",
    "PLUGIN_SOURCES",
    "PluginError",
    "HookSpec",
    "SettingSpec",
    "PythonSpec",
    "PluginManifest",
    "Plugin",
    "LoadedPlugin",
]
