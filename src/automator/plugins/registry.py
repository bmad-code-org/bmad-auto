"""PluginRegistry: the inter-pillar contract.

The registry collapses discovery + trust + in-process resolution into one
read-only object the rest of the system consumes:

  * the settings pillar reads ``settings_schema()``;
  * the hook bus reads ``hooks_for(stage)``;
  * custom orchestration reads ``provided_workflows()``.

Neither consumer reaches into loader internals. Building the registry is the
single place trust is enforced and failure is isolated:

  * a plugin with no ``[python]`` loads as a data-only/declarative LoadedPlugin
    (instance None) — its shell hooks are available to the bus;
  * a ``[python]`` plugin is constructed only if it is in ``[plugins] enabled``
    (``trust.require_enabled`` gates ``exec_module``); otherwise it is recorded
    untrusted and its module is never imported;
  * any exception while importing/constructing a trusted instance is caught
    (``except Exception`` — never ``BaseException``, so RunStopped/SIGTERM
    propagate), journalled, and the instance disabled. The run survives.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

from . import trust
from .loader import load_plugins
from .model import (
    HookSpec,
    LoadedPlugin,
    Plugin,
    PluginManifest,
    SettingSpec,
)


def _instantiate(manifest: PluginManifest) -> Plugin:
    """Import the plugin's module and construct its Plugin subclass.

    Caller is responsible for the trust gate; this performs the actual
    ``exec_module``. Kept tiny so the registry's try/except wraps exactly the
    import + construct surface.
    """
    module_path = Path(manifest.scripts_dir) / manifest.python.module  # type: ignore[union-attr]
    if not module_path.is_file():
        raise FileNotFoundError(f"plugin module not found: {module_path}")
    spec = importlib.util.spec_from_file_location(f"automator_plugin_{manifest.name}", module_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin module: {module_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    cls_name = manifest.python.cls  # type: ignore[union-attr]
    cls = getattr(module, cls_name, None)
    if cls is None:
        raise AttributeError(f"plugin module {manifest.python.module!r} has no {cls_name!r}")
    if not (isinstance(cls, type) and issubclass(cls, Plugin)):
        raise TypeError(f"{cls_name!r} must subclass plugins.Plugin")
    return cls(manifest, dict(manifest.setting_defaults()))


def _resolve(manifest: PluginManifest, policy, journal) -> LoadedPlugin:
    if manifest.python is None:
        if journal is not None:
            journal.append("plugin-loaded", plugin=manifest.name, mode="declarative")
        return LoadedPlugin(manifest=manifest)

    if not trust.is_enabled(policy, manifest.name):
        if journal is not None:
            journal.append(
                "plugin-untrusted",
                plugin=manifest.name,
                reason="[python] module requires [plugins] enabled",
            )
        return LoadedPlugin(manifest=manifest, trusted=False)

    try:
        instance = _instantiate(manifest)
    except Exception as e:  # noqa: BLE001 - isolate plugin failures; never BaseException
        if journal is not None:
            journal.append("plugin-error", plugin=manifest.name, error=f"{type(e).__name__}: {e}")
        return LoadedPlugin(manifest=manifest, disabled=True, error=str(e))

    if journal is not None:
        journal.append("plugin-loaded", plugin=manifest.name, mode="python")
    return LoadedPlugin(manifest=manifest, instance=instance)


class PluginRegistry:
    """Read-only view over the loaded plugins. Build once per run."""

    def __init__(self, loaded: list[LoadedPlugin]):
        # stable order: manifest priority then load (discovery/overlay) order.
        self._loaded = sorted(loaded, key=lambda lp: lp.manifest.priority)

    @classmethod
    def build(cls, project: Path | None = None, policy=None, journal=None) -> PluginRegistry:
        manifests = load_plugins(project, journal=journal)
        loaded = [_resolve(m, policy, journal) for m in manifests.values()]
        return cls(loaded)

    # ----------------------------------------------------------- consumers

    def plugins(self) -> list[LoadedPlugin]:
        return list(self._loaded)

    def get(self, name: str) -> LoadedPlugin | None:
        for lp in self._loaded:
            if lp.manifest.name == name:
                return lp
        return None

    def hooks_for(self, stage: str) -> list[tuple[LoadedPlugin, HookSpec]]:
        """Every (plugin, hook) bound to ``stage``, in registry order. A
        disabled instance still contributes its *declarative* hooks (those are
        out-of-process and independent of the in-process module that failed)."""
        out: list[tuple[LoadedPlugin, HookSpec]] = []
        for lp in self._loaded:
            hook = lp.manifest.hook_for(stage)
            if hook is not None:
                out.append((lp, hook))
        return out

    def settings_schema(self) -> list[tuple[str, tuple[SettingSpec, ...]]]:
        """(plugin name, setting specs) for every plugin that contributes
        settings, in registry order. Consumed by the settings pillar."""
        return [
            (lp.manifest.name, lp.manifest.settings) for lp in self._loaded if lp.manifest.settings
        ]

    def provided_workflows(self) -> dict[str, tuple[str, ...]]:
        """plugin name -> declared workflow names (for custom orchestration)."""
        return {
            lp.manifest.name: lp.manifest.workflows for lp in self._loaded if lp.manifest.workflows
        }

    def instances(self) -> list[Plugin]:
        """Constructed, trusted, non-disabled in-process plugins."""
        return [lp.instance for lp in self._loaded if lp.instance is not None]
