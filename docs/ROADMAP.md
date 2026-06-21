# bmad-auto roadmap

Forward-looking work for the orchestrator itself — design intent and rationale for features
we've deliberately deferred, so the "why" survives between sessions.

Status legend: **planned** (agreed, not started) · **exploring** (shape still open) · **blocked** (waiting on an external dependency).

---

## Parallel unit execution (`[scm] max_parallel`)

**Status:** planned · **Foundation:** landed with worktree isolation (v0.4.0)

Worktree isolation (`[scm] isolation = "worktree"`) already gives each story/bundle its own
worktree and branch, and the `max_parallel` knob is parsed and validated in
`src/automator/policy.py` (`ScmPolicy`). But it is **clamped to `1` in `loads()`** — merge-back
is serialized, one unit at a time — because the internal fan-out scheduler isn't built yet. The
knob exists so the config surface is stable; it stays inert until this phase lands.

The goal is to drive N units concurrently (each in its own worktree, independent tmux session),
then serialize only the merge-back into the target branch. Then lifting the clamp activates the
existing knob with no config change for users.

**Open questions:** how to bound concurrent CLI sessions vs. token/cost budgets; merge-back
ordering and conflict handling when several units finish close together; how the TUI surfaces
multiple in-flight units per run.

---

## Automate epic retro action items

**Status:** planned · **Blocked-by:** retro-item detail isn't standardized yet

The parser now recognizes `epic-{N}-retro-item-{M}-{slug}` keys in `sprint-status.yaml`
(`src/automator/sprintstatus.py` → `RetroItem` / `SprintStatus.retro_items`), so the
`sprint-status-unknown-keys` warning no longer fires. They are tracked but **not driven** as work.

The goal is to run actionable (`backlog`) retro items through the dev → review → commit pipeline,
the same way deferred-work sweeps already run.

**Approach (designed, not built):** a separate `bmad-auto retro` run type that mirrors the
`SweepEngine` (`src/automator/sweep.py`) end-to-end — `RetroEngine`, a `retro` CLI command + resume
branch, a `--retro-item <intent>` mode on `bmad-auto-dev`, and `verify` helpers paralleling the
bundle verifiers. Story runs stay untouched.

**Why blocked:** retro-item _detail_ is scattered — some lives in the epic retro-doc Action-Items
table (`epic-N-retro-YYYY-MM-DD.md`), some in `deferred-work.md` (DW-N) entries, some in ad-hoc
`spec-*.md` files; only one epic has an `epic-N-action-items.md`. A deterministic key→file map isn't
viable, so automation needs an LLM triage step (like sweep's) to locate/extract each item's intent
**and** classify out the non-code items (research, docs). **Prerequisite:** standardize where
retro-item detail is written at retrospective time (a future BMAD update) — that makes the triage
reliable enough to trust unattended.

---

## Integrate BMAD test-design + test-automation runs (TEA / testarch)

**Status:** exploring · **Foundation:** experimental opt-in `tea` plugin landed (v0.5.1)

The bundled, **experimental** `tea` plugin (`[plugins] enabled = ["tea"]`, see the
[TEA plugin guide](tea-plugin-guide.md)) now wires the BMAD Test Architect (TEA) suite —
`bmad-testarch-test-design`, `-automate`, `-atdd`, `-nfr`, `-trace`, `-test-review`, and the
`bmad-tea` agent — into every run and sweep as **advisory-by-default** quality steps (the three
gate steps can be flipped to blocking). It's experimental: the workflows ride the generic
`[workflows.<name>]` session-injection layer rather than being first-class orchestrated runs.

The remaining work is to drive **test design** (derive a test plan / coverage map for a feature or
backlog) and **test automation** (generate + run the actual tests) as first-class orchestrated runs —
closing the loop that retro items like `epic-5-retro-item-1-test-design-and-backfill-prior-epics`
currently call out by hand.

**Open questions:** is this a new `test` run type, or a phase wired into the existing story/review
pipeline? How does generated-test output feed verification (gate a story on its test plan / coverage)?
Which testarch skills become orchestrated vs. stay interactive?

---

## Integrate BMAD GDS game-test items

**Status:** exploring · **Foundation:** opt-in game-engine layer landed (Unity, shared + per_worktree), now riding the general plugin system

The opt-in Unity plugin (`docs/FEATURES.md` → "Game-engine projects"), enabled with
`[plugins] enabled = ["unity"]`, already lets a Unity project run its dev/sweep cycle against a
live Editor MCP in **shared** mode (agent works in place on the operator's open Editor; a readiness
gate blocks until the Editor + MCP are up) and in **per_worktree** mode (one managed Editor per
worktree, with reflink/CoW `Library` priming, setup/teardown hooks, and MCP-skill seeding via
`seed_globs`). The game-engine layer is no longer bespoke core code — it's a plugin built on the
general [plugin system](plugin-authoring-guide.md) (the legacy `[engine]` block is a deprecated
compatibility shim, folded onto `[plugins.unity]` at load time). Next steps: batchmode `verify_cmd`
and Godot/Unreal plugins on the same `plugin.toml`+scripts shape — the authoring path is documented
in [Writing a Game Engine plugin](game-engine-plugin-guide.md) and
[Writing a plugin for a specific Editor MCP](game-engine-mcp-guide.md).

The BMAD **GDS** module (game dev — Unity / Unreal / Godot) carries its own testing track via the
`gametest` workflow (`_bmad/gds/workflows/gametest`). For game projects, the testarch/TEA pipeline
above doesn't map cleanly; GDS has its own design → technical → production → gametest flow.

The goal is to let bmad-auto recognize and drive GDS game-test items the same way it drives
sprint stories and (eventually) retro items, so game projects get the same unattended
implement → test → review loop.

**Open questions:** how do GDS workflow artifacts map onto the orchestrator's sprint-status/work-item
model? Does GDS need its own run type, or can the test-design/automation integration above generalize
to cover it? Depends on the testarch integration landing first.
