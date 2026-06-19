"""Declarative game-engine plugins (Unity first; Godot/Unreal later).

A game-engine plugin adapts the bmad-auto dev/sweep cycle to a project whose
work needs a live engine Editor — e.g. a Unity project the agent drives through
an Editor MCP. It is a niche, opt-in layer (`[engine] name = "unity"`): normal
projects leave it disabled and nothing changes.

Like CLI profiles (adapters/profile.py), plugins ship as packaged TOML
(automator/data/engines/<name>/engine.toml) overlaid by project-local
<project>/.automator/engines/<name>/engine.toml (same name overrides, new names
extend). A plugin declares shell command templates for the lifecycle hooks the
engine runs:

  - ready_cmd             block until the Editor + MCP are ready (readiness gate)
  - worktree_setup_cmd    make a fresh worktree a usable engine project (per_worktree)
  - worktree_teardown_cmd tear that down on unit completion (per_worktree)
  - verify_cmd            optional batchmode build/test gate

Command templates may use ``{scripts}``, which the engine expands to the
plugin's helper-script directory (the bundled engine dir, or the project-local
override dir). Helper scripts read the ``BMAD_AUTO_*`` environment the engine
injects when it runs a hook. Resolving bundled scripts to a real filesystem
path assumes a regular (non-zipped) install — the same assumption the rest of
the package makes when it reads packaged skills/profiles.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

EDITOR_MODES = {"shared", "per_worktree"}
USER_ENGINES_REL = Path(".automator") / "engines"


class EngineError(Exception):
    pass


@dataclass(frozen=True)
class EnginePlugin:
    name: str
    # which editor_modes this plugin supports; the operator picks one in policy.
    editor_modes: tuple[str, ...] = ("shared", "per_worktree")
    ready_cmd: str = ""
    worktree_setup_cmd: str = ""
    worktree_teardown_cmd: str = ""
    verify_cmd: str = ""
    # extra gitignored paths to copy into a per_worktree checkout (e.g. the MCP
    # server's generated skill dir + client config). seed_globs are expanded
    # relative to the main repo; seed_files are literal project-relative paths.
    seed_files: tuple[str, ...] = ()
    seed_globs: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)
    # absolute path to this plugin's helper-script dir, substituted for {scripts}
    # in command templates. Set by the loader from the plugin's on-disk origin.
    scripts_dir: str = ""

    def render(self, template: str) -> str:
        """Expand ``{scripts}`` in a command template to this plugin's dir."""
        return template.replace("{scripts}", self.scripts_dir)


def _parse_plugin(doc: dict, source: str, scripts_dir: str) -> EnginePlugin:
    def fail(msg: str) -> EngineError:
        return EngineError(f"engine {source}: {msg}")

    name = str(doc.get("name", "")).strip()
    if not name:
        raise fail("'name' is required")

    editor_modes = tuple(str(m) for m in doc.get("editor_modes", ("shared", "per_worktree")))
    if not editor_modes:
        raise fail("editor_modes must list at least one mode")
    bad = sorted(set(editor_modes) - EDITOR_MODES)
    if bad:
        raise fail(f"editor_modes must be a subset of {sorted(EDITOR_MODES)}: got {bad}")

    seed_files = tuple(str(s) for s in doc.get("seed_files", ()))
    for seed in seed_files:
        if not seed or Path(seed).is_absolute():
            raise fail(f"seed_files entries must be project-relative paths: got {seed!r}")
    seed_globs = tuple(str(s) for s in doc.get("seed_globs", ()))
    for glob in seed_globs:
        if not glob or Path(glob).is_absolute():
            raise fail(f"seed_globs entries must be project-relative patterns: got {glob!r}")

    return EnginePlugin(
        name=name,
        editor_modes=editor_modes,
        ready_cmd=str(doc.get("ready_cmd", "")),
        worktree_setup_cmd=str(doc.get("worktree_setup_cmd", "")),
        worktree_teardown_cmd=str(doc.get("worktree_teardown_cmd", "")),
        verify_cmd=str(doc.get("verify_cmd", "")),
        seed_files=seed_files,
        seed_globs=seed_globs,
        env={str(k): str(v) for k, v in doc.get("env", {}).items()},
        scripts_dir=scripts_dir,
    )


def _load_toml(text: str, source: str, scripts_dir: str) -> EnginePlugin:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise EngineError(f"engine {source}: invalid TOML: {e}") from e
    return _parse_plugin(doc, source, scripts_dir)


def load_engines(project: Path | None = None) -> dict[str, EnginePlugin]:
    """Packaged built-ins, overlaid by <project>/.automator/engines/*/engine.toml.

    Each plugin lives in its own directory holding engine.toml plus any helper
    scripts; the directory is the plugin's ``{scripts}`` dir.
    """
    engines: dict[str, EnginePlugin] = {}
    packaged = resources.files("automator.data").joinpath("engines")
    if packaged.is_dir():
        for entry in sorted(packaged.iterdir(), key=lambda e: e.name):
            toml = entry.joinpath("engine.toml")
            if entry.is_dir() and toml.is_file():
                plugin = _load_toml(
                    toml.read_text(encoding="utf-8"),
                    f"{entry.name}/engine.toml",
                    str(entry),
                )
                engines[plugin.name] = plugin
    if project is not None:
        user_dir = project / USER_ENGINES_REL
        if user_dir.is_dir():
            for entry in sorted(user_dir.iterdir()):
                toml = entry / "engine.toml"
                if entry.is_dir() and toml.is_file():
                    plugin = _load_toml(toml.read_text(encoding="utf-8"), str(toml), str(entry))
                    engines[plugin.name] = plugin
    return engines


def get_engine(name: str, project: Path | None = None) -> EnginePlugin:
    engines = load_engines(project)
    plugin = engines.get(name)
    if plugin is None:
        raise EngineError(f"unknown engine plugin: {name!r} (available: {sorted(engines)})")
    return plugin
