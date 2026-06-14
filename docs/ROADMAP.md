# bmad-auto roadmap

Forward-looking work for the orchestrator itself — design intent and rationale for features
we've deliberately deferred, so the "why" survives between sessions.

Status legend: **planned** (agreed, not started) · **exploring** (shape still open) · **blocked** (waiting on an external dependency).

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

**Status:** exploring

BMAD ships a Test Architect (TEA) suite the orchestrator doesn't yet use:
`bmad-testarch-test-design`, `bmad-testarch-automate`, plus `-atdd`, `-ci`, `-framework`, `-nfr`,
`-trace`, `-test-review`, and the `bmad-tea` agent. Today bmad-auto orchestrates only
`dev / review / resolve / sweep / setup`.

The goal is to drive **test design** (derive a test plan / coverage map for a feature or backlog) and
**test automation** (generate + run the actual tests) as first-class orchestrated runs — closing the
loop that retro items like `epic-5-retro-item-1-test-design-and-backfill-prior-epics` currently call
out by hand.

**Open questions:** is this a new `test` run type, or a phase wired into the existing story/review
pipeline? How does generated-test output feed verification (gate a story on its test plan / coverage)?
Which testarch skills become orchestrated vs. stay interactive?

---

## Integrate BMAD GDS game-test items

**Status:** exploring

The BMAD **GDS** module (game dev — Unity / Unreal / Godot) carries its own testing track via the
`gametest` workflow (`_bmad/gds/workflows/gametest`). For game projects, the testarch/TEA pipeline
above doesn't map cleanly; GDS has its own design → technical → production → gametest flow.

The goal is to let bmad-auto recognize and drive GDS game-test items the same way it drives
sprint stories and (eventually) retro items, so game projects get the same unattended
implement → test → review loop.

**Open questions:** how do GDS workflow artifacts map onto the orchestrator's sprint-status/work-item
model? Does GDS need its own run type, or can the test-design/automation integration above generalize
to cover it? Depends on the testarch integration landing first.
