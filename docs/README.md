# bmad-auto documentation

Start with the [project README](../README.md) for the overview and quick start. The
guides below go deeper, roughly in the order you'll need them.

## Using bmad-auto

- **[Setup guide](setup-guide.md)** — install the tools, pick a CLI, initialize a project, and pass preflight.
- **[TUI guide](tui-guide.md)** — the dashboard: layout, key bindings, the settings editor, and troubleshooting.
- **[Features & functionality](FEATURES.md)** — the full capability matrix and policy reference.

## Extending bmad-auto

- **[Writing a bmad-auto plugin](plugin-authoring-guide.md)** — the plugin system: `plugin.toml` manifest, hooks, lifecycle stages, settings, the trust model, and workflow injection, with a worked walkthrough.
- **[Writing a Game Engine plugin](game-engine-plugin-guide.md)** — the game-engine layer (built on the plugin system): driving a live engine Editor, the `editor_mode` ↔ `[scm] isolation` coupling, a minimal Godot example.
- **[Writing a plugin for a specific Editor MCP](game-engine-mcp-guide.md)** — Editor-MCP specifics for the bundled Unity plugin: IvanMurzak vs CoplayDev, readiness probes, `per_worktree` isolation, and the full `BMAD_AUTO_*` env-var reference.

## Project direction

- **[Roadmap](ROADMAP.md)** — planned and intentionally-deferred work.

For released changes, see the [CHANGELOG](../CHANGELOG.md).
