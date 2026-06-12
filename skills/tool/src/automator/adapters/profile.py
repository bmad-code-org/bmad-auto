"""Declarative CLI profiles for the generic tmux adapter.

A profile captures everything that differs between coding CLIs that share the
tmux-injection + hook-signal transport: binary name, how the canonical
"/skill args" prompt is rendered, bypass flags, hook registration (a config
dialect + an event-name map), and which usage parser reads the transcript.

Built-in profiles ship as packaged TOML (automator/data/profiles/*.toml) and
project-local TOML files in <project>/.automator/profiles/*.toml overlay them
(same name overrides, new names extend) — adding a CLI that clones an
existing hook dialect needs no Python.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

USAGE_PARSERS = {"claude-jsonl", "codex-rollout", "gemini-chat", "none"}
HOOK_DIALECTS = {"claude-settings-json", "codex-hooks-json", "gemini-settings-json"}
CANONICAL_EVENTS = {"SessionStart", "Stop", "SessionEnd", "PreCompact"}
USER_PROFILES_REL = Path(".automator") / "profiles"

# legacy adapter names from older policy.toml files
ALIASES = {"claude-code-tmux": "claude"}


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class HookSpec:
    dialect: str
    config_path: str  # project-relative, e.g. ".claude/settings.json"
    events: dict[str, str]  # native event name -> canonical event name


@dataclass(frozen=True)
class CLIProfile:
    name: str
    binary: str
    hooks: HookSpec
    prompt_template: str = "{prompt}"
    launch_args: tuple[str, ...] = ()
    bypass_args: tuple[str, ...] = ()
    model_flag: str = "--model"
    env: dict[str, str] = field(default_factory=dict)
    usage_parser: str = "none"
    first_run_note: str = ""

    def render_prompt(self, prompt: str) -> str:
        """Render the engine's canonical "/skill args" prompt for this CLI.

        Placeholders: {prompt} = the canonical string, {skill} = the leading
        slash-command name without "/", {args} = everything after it.
        """
        skill, args = "", prompt
        if prompt.startswith("/"):
            head, _, rest = prompt[1:].partition(" ")
            skill, args = head, rest.strip()
        return self.prompt_template.format(prompt=prompt, skill=skill, args=args)


def _parse_profile(doc: dict, source: str) -> CLIProfile:
    def fail(msg: str) -> ProfileError:
        return ProfileError(f"profile {source}: {msg}")

    name = str(doc.get("name", "")).strip()
    binary = str(doc.get("binary", "")).strip()
    if not name or not binary:
        raise fail("'name' and 'binary' are required")

    hooks_d = doc.get("hooks")
    if not isinstance(hooks_d, dict):
        raise fail("missing [hooks] table")
    dialect = str(hooks_d.get("dialect", ""))
    if dialect not in HOOK_DIALECTS:
        raise fail(f"hooks.dialect must be one of {sorted(HOOK_DIALECTS)}: got {dialect!r}")
    config_path = str(hooks_d.get("config_path", ""))
    if not config_path or Path(config_path).is_absolute():
        raise fail("hooks.config_path must be a project-relative path")
    events_d = hooks_d.get("events")
    if not isinstance(events_d, dict) or not events_d:
        raise fail("hooks.events must map native event names to canonical ones")
    events = {str(k): str(v) for k, v in events_d.items()}
    bad = sorted(set(events.values()) - CANONICAL_EVENTS)
    if bad:
        raise fail(f"hooks.events values must be canonical {sorted(CANONICAL_EVENTS)}: got {bad}")

    usage_parser = str(doc.get("usage_parser", "none"))
    if usage_parser not in USAGE_PARSERS:
        raise fail(f"usage_parser must be one of {sorted(USAGE_PARSERS)}: got {usage_parser!r}")

    return CLIProfile(
        name=name,
        binary=binary,
        hooks=HookSpec(dialect=dialect, config_path=config_path, events=events),
        prompt_template=str(doc.get("prompt_template", "{prompt}")),
        launch_args=tuple(str(a) for a in doc.get("launch_args", ())),
        bypass_args=tuple(str(a) for a in doc.get("bypass_args", ())),
        model_flag=str(doc.get("model_flag", "--model")),
        env={str(k): str(v) for k, v in doc.get("env", {}).items()},
        usage_parser=usage_parser,
        first_run_note=str(doc.get("first_run_note", "")),
    )


def _load_toml(text: str, source: str) -> CLIProfile:
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ProfileError(f"profile {source}: invalid TOML: {e}") from e
    return _parse_profile(doc, source)


def load_profiles(project: Path | None = None) -> dict[str, CLIProfile]:
    """Packaged built-ins, overlaid by <project>/.automator/profiles/*.toml."""
    profiles: dict[str, CLIProfile] = {}
    packaged = resources.files("automator.data").joinpath("profiles")
    for entry in sorted(packaged.iterdir(), key=lambda e: e.name):
        if entry.name.endswith(".toml"):
            profile = _load_toml(entry.read_text(encoding="utf-8"), entry.name)
            profiles[profile.name] = profile
    if project is not None:
        user_dir = project / USER_PROFILES_REL
        if user_dir.is_dir():
            for path in sorted(user_dir.glob("*.toml")):
                profile = _load_toml(path.read_text(encoding="utf-8"), str(path))
                profiles[profile.name] = profile
    return profiles


def get_profile(name: str, project: Path | None = None) -> CLIProfile:
    profiles = load_profiles(project)
    profile = profiles.get(ALIASES.get(name, name))
    if profile is None:
        raise ProfileError(f"unknown CLI profile: {name!r} (available: {sorted(profiles)})")
    return profile
