# Writing a Game Engine plugin

The **Game Engine** layer adapts the bmad-auto dev/sweep cycle to projects whose
work needs a **live engine Editor** — e.g. a Unity project the agent drives through
an Editor MCP. It is niche and **opt-in**: a normal project enables no engine
plugin and the orchestrator behaves exactly as before.

As of the plugin-system migration, **the game engine is just a plugin** on the
general [plugin system](plugin-authoring-guide.md). There is no separate engine
machinery: an engine plugin uses the same `plugin.toml` manifest, the same
[lifecycle hooks](plugin-authoring-guide.md#stage-reference), and the same trust
model as any other plugin. This guide covers the **engine-specific** slice —
which stages an Editor binds, the `editor_mode` ↔ isolation coupling, and the env
a readiness/setup script reads. **Read the [plugin authoring
guide](plugin-authoring-guide.md) first** for the manifest, settings, hook, and
trust fundamentals.

Unity ships bundled as the reference engine plugin
(`src/automator/data/plugins/unity/`). This guide is for adding **another engine**
(Godot, Unreal, …) — or reshaping the Unity one for your project. For wiring a
specific Editor MCP (IvanMurzak vs CoplayDev, readiness probing, the full env-var
reference), see the companion [Game Engine MCP guide](game-engine-mcp-guide.md).

> If you can write a shell/Python command that exits `0` when your Editor + MCP are
> ready, you can write an engine plugin — no in-process code required.

## How an engine plugin is loaded

Like any plugin, it's a directory with a `plugin.toml` (plus helper scripts),
discovered and overlaid from:

| Source        | Path                                              | Wins         |
| ------------- | ------------------------------------------------- | ------------ |
| Bundled       | `automator/data/plugins/<name>/plugin.toml`       | base         |
| Project-local | `<project>/.automator/plugins/<name>/plugin.toml` | **override** |

A project-local plugin with the **same name** overrides the bundled one. The
plugin's directory is its `{scripts}` dir, so its manifest and helper scripts sit
together.

Enable it in `.automator/policy.toml`:

```toml
[plugins]
enabled = ["unity"]          # or your engine's name

[plugins.unity]
editor_mode = "shared"
mcp = "ivanmurzak"
```

> **Legacy `[engine]` still works.** A pre-migration `[engine] name = "unity"`
> block loads with a deprecation warning, folded into the `[plugins]` allowlist
> plus a `[plugins.unity]` table. The _policy block_ is the only thing folded,
> though — project-local plugin overrides are now discovered under
> `.automator/plugins/<name>/`, so move an old `.automator/engines/unity/`
> override dir to `.automator/plugins/unity/`. Migrate to `[plugins]` when
> convenient.

## Mapping the Editor lifecycle onto hook stages

An engine binds the orchestrator's **per-story stages** that surround a unit's
worktree and sessions. The relevant ones (full list in the
[stage reference](plugin-authoring-guide.md#stage-reference)):

| Stage                   | shared mode                       | per_worktree mode                                                                        |
| ----------------------- | --------------------------------- | ---------------------------------------------------------------------------------------- |
| `pre_worktree_setup`    | not run                           | per unit, right after the worktree is cut — make it a usable project + launch its Editor |
| `pre_ready_gate`        | once, before the first session    | per unit, after setup, before the agent runs — block until Editor + MCP are ready        |
| (agent dev/review)      | drives the operator's live Editor | drives the worktree's managed Editor                                                     |
| `pre_worktree_teardown` | not run                           | per unit, on completion **and** on pause/escalation — quit the Editor + clean up         |

A **blocking** hook at `pre_ready_gate` or `pre_worktree_setup` whose command
exits non-zero **defers the unit** — bmad-auto never starts a session against a
half-open Editor. `pre_worktree_teardown` is **observe-only** for veto purposes
(a veto can't un-tear-down) but the command still **runs** — best-effort, even
when a unit pauses or escalates, so a managed Editor never outlives its worktree.

You can implement these as **declarative** `[hooks.<stage>]` shell commands (the
smallest thing that works), or as an **in-process** `[python]` module when you need
richer logic. The bundled Unity plugin is in-process because it also does MCP
agent routing, `editor_mode`↔isolation validation, and Library priming — but a
simple engine needs none of that.

## The `editor_mode` ↔ `[scm] isolation` coupling

A live Editor MCP can only act on the folder its Editor has open, and most engines
bind one Editor per folder and can't be repointed live. So an engine's
`editor_mode` setting is coupled to `[scm] isolation`:

- **`shared`** requires `[scm] isolation = "none"` — the agent works **in place**
  on the project your warm Editor already has open. Zero relaunches, full live MCP,
  the Editor stays open across stories. The recommended starting point.
- **`per_worktree`** requires `[scm] isolation = "worktree"` — one **managed Editor
  per worktree**, run serially, each launched by your `pre_worktree_setup` hook.

The bundled Unity plugin **enforces** this coupling in its `validate(policy)`
(raising at startup on a mismatch — e.g. `editor_mode = "per_worktree"` with
`isolation = "none"`), and the TUI surfaces it on save. An engine plugin you write
should validate the same way (see
[`Plugin.validate`](plugin-authoring-guide.md#in-process-hooks)).

**Start with `shared` only.** A new engine plugin can support just `shared` and a
single `pre_ready_gate` hook — skip setup/teardown entirely. Add `per_worktree`
once the in-place flow is solid.

## The environment a hook script reads

A declarative hook receives the **generic bus environment** (full table in the
[authoring guide](plugin-authoring-guide.md#declarative-hooks)) — the run/unit
identity plus **`BMAD_AUTO_SETTING_<KEY>`** for each of your `[[settings]]`. So a
readiness script reads its knobs from its own settings:

| Variable                  | Source                                    |
| ------------------------- | ----------------------------------------- |
| `BMAD_AUTO_WORKTREE`      | the workspace/worktree the Editor opens   |
| `BMAD_AUTO_REPO_ROOT`     | main repo root                            |
| `BMAD_AUTO_STORY_KEY`     | the current story key                     |
| `BMAD_AUTO_SETTING_<KEY>` | each of your plugin's settings (resolved) |

The bundled Unity plugin's in-process module additionally exports
`BMAD_AUTO_ENGINE_MCP`, `BMAD_AUTO_ENGINE_EDITOR_MODE`,
`BMAD_AUTO_ENGINE_READY_TIMEOUT`, `BMAD_AUTO_ENGINE_READY_GRACE`, and
`BMAD_AUTO_UNITY_PATH` for its bundled scripts (derived from its settings) — a
plugin-internal contract, not part of the generic env. The
[Game Engine MCP guide](game-engine-mcp-guide.md) tables every knob the Unity
scripts read.

## Worked example: a minimal `shared`-mode Godot plugin

The smallest useful engine plugin is a single readiness gate. Drop two files under
`<project>/.automator/plugins/godot/`:

`plugin.toml`:

```toml
[plugin]
name = "godot"
version = "1.0.0"
api_version = 1
description = "Drive a Godot project that needs a live Editor + MCP."

[[settings]]
key = "mcp_url"
type = "str"
default = "http://localhost:9000"
label = "Godot MCP URL"
help = "Where the readiness probe connects."

[[settings]]
key = "ready_timeout_sec"
type = "int"
default = 600
label = "Readiness timeout (sec)"

# Readiness gate: block until the Editor + MCP answer. A non-zero exit defers
# the unit, so a session never starts against a half-open Editor.
[hooks.pre_ready_gate]
cmd = 'python3 "{scripts}/godot_ready.py"'
blocking = true
timeout_sec = 600
```

`godot_ready.py` (exit `0` when the Editor + MCP answer, non-zero otherwise):

```python
#!/usr/bin/env python3
import os, sys, time, socket
from urllib.parse import urlparse

url = os.environ.get("BMAD_AUTO_SETTING_MCP_URL", "http://localhost:9000")
deadline = time.time() + int(os.environ.get("BMAD_AUTO_SETTING_READY_TIMEOUT_SEC", "600"))

host, port = urlparse(url).hostname, urlparse(url).port or 80
while time.time() < deadline:
    try:
        with socket.create_connection((host, port), timeout=2):
            sys.exit(0)                          # ready
    except OSError:
        time.sleep(2)
sys.exit(1)                                       # never came up → unit deferred
```

Then enable it — and keep `[scm] isolation = "none"` (the default) for `shared`:

```toml
[plugins]
enabled = ["godot"]
```

That's a complete engine plugin. To give each unit its own Editor, add
`[hooks.pre_worktree_setup]` + `[hooks.pre_worktree_teardown]` and switch
`[scm] isolation = "worktree"` — see the MCP guide for the per-worktree
port-isolation and seeding mechanics. If you need the `editor_mode`↔isolation
validation or MCP agent routing the Unity plugin does, reach for a `[python]`
module (see the [authoring guide](plugin-authoring-guide.md#in-process-hooks)).

> A **declarative** engine plugin activates as soon as its folder is present (the
> declarative trust tier). For an engine that's usually what you want. If you'd
> rather require explicit opt-in via `[plugins] enabled`, give the plugin a
> `[python]` module — that's trust-gated and won't run until listed. The bundled
> Unity plugin is in-process for exactly this reason.

## Reference: the bundled Unity plugin

The canonical example lives at `src/automator/data/plugins/unity/`:

- `plugin.toml` — a `[python]` module + five `[[settings]]` (`editor_mode`, `mcp`,
  `unity_path`, `ready_timeout_sec`, `ready_grace_sec`) + `seed_globs =
[".claude/skills/*"]`.
- `unity_plugin.py` — the in-process brain: the readiness gate
  (`on_pre_ready_gate`), `per_worktree` Editor setup/teardown, MCP agent routing,
  Library priming, and the `editor_mode`↔`scm.isolation` coupling validation.
- `unity_ready.py` — readiness gate script (branches on `BMAD_AUTO_ENGINE_MCP`).
- `unity_setup.py` — `per_worktree` Library priming, `.mcp.json` write, Custom-mode
  pin, and Editor launch.
- `unity_teardown.py` — Editor quit + MCP-server reap + symlink-Library cleanup.

Each script's module docstring documents every env knob it reads — the
authoritative source if a default ever changes. The [Game Engine MCP guide](game-engine-mcp-guide.md)
distills those into a single reference table and explains the IvanMurzak vs
CoplayDev differences.
