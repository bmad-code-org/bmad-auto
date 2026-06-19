# Writing a Game Engine plugin

The **Game Engine** layer (`[engine]` in `.automator/policy.toml`) adapts the
bmad-auto dev/sweep cycle to projects whose work needs a **live engine Editor** —
e.g. a Unity project the agent drives through an Editor MCP. It is niche and
**opt-in**: a normal project leaves `[engine] name = ""` and the orchestrator
behaves exactly as before.

Unity ships bundled as the reference plugin. This guide is for adding **another
engine** (Godot, Unreal, …) — or reshaping the Unity one for your project. For
wiring a specific Editor MCP (IvanMurzak vs CoplayDev, readiness probing, the full
env-var reference), see the companion [Game Engine MCP guide](game-engine-mcp-guide.md).

> A plugin is pure data + scripts — no Python. If you can write a shell/Python
> command that exits `0` when your Editor + MCP are ready, you can write a plugin.

## How plugins are loaded

Plugins work exactly like [CLI adapter profiles](../README.md#other-coding-clis):
a declarative TOML file plus optional helper scripts, discovered from two places
and overlaid:

| Source        | Path                                              | Wins         |
| ------------- | ------------------------------------------------- | ------------ |
| Bundled       | `automator/data/engines/<name>/engine.toml`       | base         |
| Project-local | `<project>/.automator/engines/<name>/engine.toml` | **override** |

A project-local plugin with the **same name** overrides the bundled one; a **new
name** extends the set. Each plugin lives in its **own directory** — that directory
is the plugin's `{scripts}` dir (see below), so its `engine.toml` and helper scripts
sit together. Enable a plugin by name in policy:

```toml
[engine]
name = "godot"            # matches the directory name under .automator/engines/
editor_mode = "shared"
```

(See `src/automator/engines/plugin.py` for the loader — `load_engines()` /
`get_engine()`.)

## `engine.toml` schema

Every field maps to the `EnginePlugin` dataclass (`engines/plugin.py`). Only
`name` is required; everything else defaults to empty/no-op, so a plugin opts into
exactly the lifecycle hooks it needs.

| Field                   | Type           | Default                     | Purpose                                                                                                |
| ----------------------- | -------------- | --------------------------- | ------------------------------------------------------------------------------------------------------ |
| `name`                  | string         | —                           | **Required.** Plugin id; must match the policy `engine.name`.                                          |
| `editor_modes`          | list of string | `["shared","per_worktree"]` | Which modes this plugin supports; the operator picks one in policy.                                    |
| `ready_cmd`             | string         | `""`                        | Readiness gate — block until Editor + MCP are ready.                                                   |
| `worktree_setup_cmd`    | string         | `""`                        | `per_worktree` only — make a fresh worktree a usable project + launch its Editor.                      |
| `worktree_teardown_cmd` | string         | `""`                        | `per_worktree` only — quit that Editor + clean up.                                                     |
| `verify_cmd`            | string         | `""`                        | Optional batchmode build/test gate.                                                                    |
| `seed_files`            | list of string | `[]`                        | Project-relative gitignored files to copy into each worktree.                                          |
| `seed_globs`            | list of string | `[]`                        | Project-relative glob patterns to expand + copy into each worktree (e.g. an MCP-generated skill tree). |
| `[env]`                 | table          | `{}`                        | Extra environment variables every hook command receives — the override point for plugin/MCP knobs.     |

**`{scripts}` substitution.** Any `*_cmd` template may contain `{scripts}`, which
the engine expands to the plugin's on-disk directory. This is how a command reaches
its helper scripts regardless of where the plugin was installed:

```toml
ready_cmd = 'python3 "{scripts}/godot_ready.py"'
```

**Seed-path rules.** `seed_files` and `seed_globs` entries must be **project-relative**
(absolute paths are rejected at load). `seed_globs` are expanded relative to the main
repo; `seed_files` are literal project-relative paths. See the MCP guide for why and
when seeding matters.

## The lifecycle hooks (and when each runs)

The engine runs your command templates at fixed points, rendering `{scripts}` and
injecting the `BMAD_AUTO_*` environment first (see [env contract](#the-environment-contract)).
A command **exits `0` to proceed**; a non-zero exit (or timeout) **defers the unit**
with an `ATTENTION` notice — bmad-auto never starts a session against a half-open
Editor. (Source: `Engine._engine_ready_gate` / `_engine_worktree_setup` /
`_engine_worktree_teardown` in `src/automator/engine.py`.)

| Hook                    | shared mode                       | per_worktree mode                                   |
| ----------------------- | --------------------------------- | --------------------------------------------------- |
| `worktree_setup_cmd`    | not run                           | per unit, right after the worktree is cut           |
| `ready_cmd`             | once, before the first session    | per unit, after setup, before the agent runs        |
| (agent dev/review)      | drives the operator's live Editor | drives the worktree's managed Editor                |
| `worktree_teardown_cmd` | not run                           | per unit, on completion **and** on pause/escalation |
| `verify_cmd`            | optional, where verification runs | optional, where verification runs                   |

`worktree_teardown_cmd` is **best-effort**: it runs even when a unit pauses or
escalates so a managed Editor never outlives its worktree, and a non-zero teardown
exit is logged but does not change the unit's outcome.

## The `editor_mode` ↔ `[scm] isolation` coupling

A live Editor MCP can only act on the folder its Editor has open, and most engines
bind one Editor per folder and can't be repointed live. So `editor_mode` is coupled
to `[scm] isolation`, and the policy parser **enforces** it:

- **`shared`** requires `[scm] isolation = "none"` — the agent works **in place** on
  the project your warm Editor already has open. Zero relaunches, full live MCP, the
  Editor stays open across stories. This is the recommended starting point.
- **`per_worktree`** requires `[scm] isolation = "worktree"` — one **managed Editor
  per worktree**, run serially, each launched by your setup hook.

A mismatch (e.g. `editor_mode = "per_worktree"` with `isolation = "none"`) is a
hard policy error — both at `bmad-auto` start and on save in the TUI **Game Engine**
settings section.

**Start with `shared` only.** A new plugin can declare `editor_modes = ["shared"]`
and skip the setup/teardown hooks entirely — that's the smallest thing that works.
Add `per_worktree` once the in-place flow is solid.

## The environment contract

Before running any hook, the engine injects these variables (from the six `[engine]`
policy keys), on top of the parent environment and your plugin's `[env]` block.
Your scripts read these — they are the stable interface (source: `engine.py`):

| Variable                         | Source                                        |
| -------------------------------- | --------------------------------------------- |
| `BMAD_AUTO_REPO_ROOT`            | main repo root                                |
| `BMAD_AUTO_WORKTREE`             | the workspace/worktree the Editor should open |
| `BMAD_AUTO_RUN_DIR`              | the run directory                             |
| `BMAD_AUTO_STORY_KEY`            | the current story key                         |
| `BMAD_AUTO_ENGINE_MCP`           | `engine.mcp`                                  |
| `BMAD_AUTO_ENGINE_EDITOR_MODE`   | `engine.editor_mode`                          |
| `BMAD_AUTO_ENGINE_READY_TIMEOUT` | `engine.ready_timeout_sec`                    |
| `BMAD_AUTO_ENGINE_READY_GRACE`   | `engine.ready_grace_sec`                      |
| `BMAD_AUTO_UNITY_PATH`           | `engine.unity_path`                           |

Anything **engine-specific beyond these** (Editor CLI name, server flags, cache
locations, …) belongs in your plugin's `[env]` block — that's how operators tune a
plugin without forking its scripts. The Unity plugin exposes a large set of such
knobs; they're tabled in the [Game Engine MCP guide](game-engine-mcp-guide.md).

## Worked example: a minimal `shared`-mode plugin

The smallest useful plugin is a single readiness gate. Drop two files under
`<project>/.automator/engines/godot/`:

`engine.toml`:

```toml
name = "godot"
editor_modes = ["shared"]                       # in-place only, to start
ready_cmd = 'python3 "{scripts}/godot_ready.py"'

[env]
GODOT_MCP_URL = "http://localhost:9000"         # your knob, read by the script
```

`godot_ready.py` (exit `0` when the Editor + MCP answer, non-zero otherwise):

```python
#!/usr/bin/env python3
import os, sys, time, socket
from urllib.parse import urlparse

url = os.environ.get("GODOT_MCP_URL", "http://localhost:9000")
deadline = time.time() + int(os.environ.get("BMAD_AUTO_ENGINE_READY_TIMEOUT", "600"))
time.sleep(max(0, int(os.environ.get("BMAD_AUTO_ENGINE_READY_GRACE", "0"))))  # -1=auto: treat as 0 in shared

host, port = urlparse(url).hostname, urlparse(url).port or 80
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            sys.exit(0)                          # ready
    except OSError:
        time.sleep(2)
sys.exit(1)                                       # never came up → unit deferred
```

Then enable it:

```toml
[engine]
name = "godot"
editor_mode = "shared"     # [scm] isolation must be "none" (the default)
```

That's a complete plugin. Add `worktree_setup_cmd` / `worktree_teardown_cmd` and
`editor_modes = ["shared","per_worktree"]` when you're ready to give each unit its
own Editor — see the MCP guide for the per-worktree port-isolation and seeding
mechanics.

## Reference: the bundled Unity plugin

The canonical example lives at `src/automator/data/engines/unity/`:

- `engine.toml` — declares `ready_cmd`, `worktree_setup_cmd`,
  `worktree_teardown_cmd`, and `seed_globs = [".claude/skills/*"]`.
- `unity_ready.py` — readiness gate (branches on `BMAD_AUTO_ENGINE_MCP`).
- `unity_setup.py` — `per_worktree` Library priming, `.mcp.json` write, Custom-mode
  pin, and Editor launch.
- `unity_teardown.py` — Editor quit + MCP-server reap + symlink-Library cleanup.

Each script's module docstring documents every env knob it reads — the
authoritative source if a default ever changes. The [Game Engine MCP guide](game-engine-mcp-guide.md)
distills those into a single reference table and explains the IvanMurzak vs
CoplayDev differences.
