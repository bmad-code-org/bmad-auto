"""bmad-auto command line interface."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import (
    __version__,
    bmadconfig,
    decisions,
    deferredwork,
)
from . import policy as policy_mod
from . import (
    resolve,
    runs,
    sprintstatus,
    verify,
)
from .adapters.base import CodingCLIAdapter
from .engine import Engine
from .journal import Journal, load_state, save_state
from .model import RunState
from .runs import RUNS_DIR
from .sweep import SweepEngine

POLICY_FILE = policy_mod.POLICY_FILE


def _project(args: argparse.Namespace) -> Path:
    return Path(args.project).resolve()


def _policy_path(project: Path) -> Path:
    return project / POLICY_FILE


ROLES = ("dev", "review", "triage")


def _make_adapters(project: Path, run_dir: Path, policy) -> dict[str, CodingCLIAdapter]:
    from .adapters.generic_tmux import GenericTmuxAdapter
    from .adapters.profile import ProfileError, get_profile

    adapters: dict[str, CodingCLIAdapter] = {}
    by_cfg: dict = {}
    for role in ROLES:
        cfg = policy.adapter.resolved(role)
        if cfg not in by_cfg:
            try:
                profile = get_profile(cfg.name, project)
            except ProfileError as e:
                raise SystemExit(f"error: {e}") from e
            by_cfg[cfg] = GenericTmuxAdapter(
                run_dir=run_dir,
                policy=policy,
                profile=profile,
                extra_args=cfg.extra_args,
            )
        adapters[role] = by_cfg[cfg]
    return adapters


# ----------------------------------------------------------------- commands


def cmd_validate(args: argparse.Namespace) -> int:
    project = _project(args)
    problems: list[str] = []
    notes: list[str] = []

    try:
        paths = bmadconfig.load_paths(project)
        notes.append(f"BMAD config OK: artifacts at {paths.implementation_artifacts}")
    except bmadconfig.BmadConfigError as e:
        problems.append(str(e))
        paths = None

    if paths:
        try:
            ss = sprintstatus.load(paths.sprint_status)
            actionable = [s for s in ss.stories if s.status in sprintstatus.ACTIONABLE_STATUSES]
            notes.append(
                f"sprint-status OK: {len(ss.stories)} stories, {len(actionable)} actionable"
            )
            if ss.unknown_keys:
                notes.append(f"  warning: unknown keys ignored: {', '.join(ss.unknown_keys)}")
        except sprintstatus.SprintStatusError as e:
            problems.append(str(e))

    from .adapters.profile import ProfileError, get_profile

    profiles = []
    try:
        pol = policy_mod.load(_policy_path(project))
        role_names = {role: pol.adapter.resolved(role).name for role in ROLES}
        notes.append(
            f"policy OK: gates={pol.gates.mode}, "
            f"adapter dev={role_names['dev']}, review={role_names['review']}, "
            f"triage={role_names['triage']}"
        )
        for name in dict.fromkeys(role_names.values()):
            try:
                profiles.append(get_profile(name, project))
            except ProfileError as e:
                problems.append(str(e))
    except policy_mod.PolicyError as e:
        problems.append(str(e))
        pol = None

    try:
        if not verify.worktree_clean(project):
            problems.append("git worktree is not clean — commit or stash before running")
        else:
            notes.append("git worktree clean")
    except verify.GitError as e:
        problems.append(f"git check failed: {e}")

    tools = ("tmux", *dict.fromkeys(p.binary for p in profiles))
    for tool in tools:
        if shutil.which(tool):
            notes.append(f"{tool} found")
        else:
            problems.append(f"{tool} not found on PATH")

    for profile in profiles:
        hook_config = project / profile.hooks.config_path
        hooks_ok = False
        if hook_config.is_file():
            try:
                hooks = json.loads(hook_config.read_text(encoding="utf-8")).get("hooks", {})
                hooks_ok = any(
                    "bmad_auto_hook" in json.dumps(hooks.get(event, []))
                    for event in profile.hooks.events
                )
            except json.JSONDecodeError:
                problems.append(f"{hook_config} is not valid JSON")
        if hooks_ok:
            notes.append(f"bmad-auto hooks registered for {profile.name}")
        else:
            problems.append(
                f"bmad-auto hooks not registered for {profile.name} — "
                f"run `bmad-auto init --cli {profile.name}`"
            )

    for note in notes:
        print(f"  ok: {note}")
    for problem in problems:
        print(f"FAIL: {problem}", file=sys.stderr)
    return 1 if problems else 0


def cmd_run(args: argparse.Namespace) -> int:
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = policy_mod.load(_policy_path(project))

    if args.dry_run:
        return _dry_run(paths, pol, args)

    if not verify.worktree_clean(project):
        print("git worktree is not clean — commit or stash first", file=sys.stderr)
        return 1

    run_id = args.run_id or runs.new_run_id()
    run_dir = project / RUNS_DIR / run_id
    journal = Journal(run_dir)
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        policy_snapshot=pol.to_dict(),
    )
    save_state(run_dir, state)
    runs.write_pid(run_dir)
    adapters = _make_adapters(project, run_dir, pol)
    journal.append(
        "run-start",
        run_id=run_id,
        adapter_dev=pol.adapter.resolved("dev").name,
        adapter_review=pol.adapter.resolved("review").name,
    )
    print(f"run {run_id} starting (attach: bmad-auto attach)")

    engine = Engine(
        paths=paths,
        policy=pol,
        adapter=adapters["dev"],
        review_adapter=adapters["review"],
        run_dir=run_dir,
        journal=journal,
        state=state,
        max_stories=args.max_stories,
        epic_filter=args.epic,
        story_filter=args.story,
        sweep_factory=_sweep_factory(project, paths),
    )
    summary = engine.run()
    print(summary.render())
    return 0


def _render_invocation(pol, project: Path, role: str, prompt: str) -> str:
    from .adapters.profile import get_profile

    cfg = pol.adapter.resolved(role)
    profile = get_profile(cfg.name, project)
    extra = cfg.extra_args if cfg.extra_args is not None else profile.bypass_args
    argv = [
        profile.binary,
        *profile.launch_args,
        f'"{profile.render_prompt(prompt)}"',
        *extra,
    ]
    if cfg.model:
        argv += [profile.model_flag, cfg.model]
    return " ".join(argv)


def _dry_run(paths: bmadconfig.ProjectPaths, pol, args: argparse.Namespace) -> int:
    def render(role: str, prompt: str) -> str:
        return _render_invocation(pol, paths.project, role, prompt)

    ss = sprintstatus.load(paths.sprint_status)
    queue = [
        s
        for s in ss.stories
        if s.status in sprintstatus.ACTIONABLE_STATUSES
        and (args.epic is None or s.epic == args.epic)
        and (args.story is None or s.key == args.story)
    ]
    if args.max_stories is not None:
        queue = queue[: args.max_stories]
    if not queue:
        print("no actionable stories")
        return 0
    print(f"would process {len(queue)} stories (gates={pol.gates.mode}):")
    for story in queue:
        print(f"\n  {story.key} (epic {story.epic}, status {story.status})")
        print(f"    dev:    {render('dev', f'/bmad-auto-dev {story.key}')}")
        print(f"    review: {render('review', '/bmad-auto-review <spec from dev>')}")
        print(f"    env:    BMAD_AUTO_MODE=1 BMAD_AUTO_STORY_KEY={story.key}")
    return 0


def _start_sweep(
    project: Path,
    paths: bmadconfig.ProjectPaths,
    pol,
    *,
    prompting: bool,
    decisions_only: bool,
    max_bundles: int | None,
    repeat: bool | None = None,
    max_cycles: int | None = None,
    trigger: str,
    run_id: str | None = None,
) -> int:
    run_id = run_id or runs.new_run_id()
    run_dir = project / RUNS_DIR / run_id
    journal = Journal(run_dir)
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        policy_snapshot=pol.to_dict(),
        run_type="sweep",
    )
    save_state(run_dir, state)
    runs.write_pid(run_dir)
    options = {
        "prompting": prompting,
        "decisions_only": decisions_only,
        "max_bundles": max_bundles,
        "repeat": repeat,
        "max_cycles": max_cycles,
        "trigger": trigger,
    }
    (run_dir / "sweep.json").write_text(json.dumps(options, indent=2), encoding="utf-8")
    adapters = _make_adapters(project, run_dir, pol)
    journal.append("run-start", run_id=run_id, run_type="sweep", trigger=trigger)
    print(f"sweep {run_id} starting (attach: bmad-auto attach)")
    engine = SweepEngine(
        paths=paths,
        policy=pol,
        adapter=adapters["dev"],
        review_adapter=adapters["review"],
        triage_adapter=adapters["triage"],
        run_dir=run_dir,
        journal=journal,
        state=state,
        prompting=prompting,
        decisions_only=decisions_only,
        max_bundles=max_bundles,
        repeat=repeat,
        max_cycles=max_cycles,
    )
    summary = engine.run()
    print(summary.render())
    return 0


def _sweep_factory(project: Path, paths: bmadconfig.ProjectPaths):
    """Child-sweep launcher injected into story-run engines. Auto-triggered
    sweeps are unattended: never prompt, never run decision bundles."""

    def factory(trigger: str) -> None:
        pol = policy_mod.load(_policy_path(project))
        _start_sweep(
            project,
            paths,
            pol,
            prompting=False,
            decisions_only=False,
            max_bundles=None,
            trigger=trigger,
        )

    return factory


def cmd_sweep(args: argparse.Namespace) -> int:
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    pol = policy_mod.load(_policy_path(project))

    if args.dry_run:
        return _sweep_dry_run(paths, pol)

    if not verify.worktree_clean(project):
        print("git worktree is not clean — commit or stash first", file=sys.stderr)
        return 1

    return _start_sweep(
        project,
        paths,
        pol,
        prompting=not args.no_prompt,
        decisions_only=args.decisions_only,
        max_bundles=args.max_bundles,
        repeat=args.repeat,
        max_cycles=args.max_cycles,
        trigger="cli",
        run_id=args.run_id,
    )


def _sweep_dry_run(paths: bmadconfig.ProjectPaths, pol) -> int:
    ledger = paths.deferred_work
    if not ledger.is_file():
        print(f"no deferred-work ledger at {ledger}")
        return 0
    text = ledger.read_text(encoding="utf-8")
    entries = deferredwork.parse_ledger(text)
    open_entries = [e for e in entries if e.open]
    closed = len(entries) - len(open_entries)
    print(f"{ledger}: {len(open_entries)} open, {closed} closed/non-open")
    for entry in open_entries:
        print(f"  {entry.id:8s} {entry.title}")
    legacy = deferredwork.parse_legacy(text)
    legacy_open = [e for e in legacy if not e.done]
    if legacy:
        print(
            f"plus {len(legacy)} legacy (pre-DW-format) entries, {len(legacy_open)} open"
            " — a sweep would first migrate them to DW format"
        )
        for entry in legacy_open:
            print(f"  {entry.id or '-':8s} {entry.title}")
    if open_entries or legacy_open:
        print("a sweep would triage the open entries in one LLM session, then run bundles")
        print(f"  triage: {_render_invocation(pol, paths.project, 'triage', '/bmad-auto-sweep')}")
    return 0


def _resume_paused_run(project: Path, run_dir: Path) -> int:
    """Resume the engine for a paused/interrupted run. Shared by `resume` and
    the re-arm step of `resolve`."""
    paths = bmadconfig.load_paths(project)
    state = load_state(run_dir)
    if state.finished:
        print(f"run {run_dir.name} already finished", file=sys.stderr)
        return 1
    pol = policy_mod.load(_policy_path(project))
    journal = Journal(run_dir)
    journal.append("run-resume", was_paused=state.paused_reason)
    state.clear_pause()
    runs.write_pid(run_dir)
    # drop any stale agent session so the run spins up a fresh one (a stopped or
    # interrupted run can leave a lingering bmad-auto-<id> session behind).
    runs.kill_session(run_dir.name)
    adapters = _make_adapters(project, run_dir, pol)
    if state.run_type == "sweep":
        opts_path = run_dir / "sweep.json"
        opts = json.loads(opts_path.read_text(encoding="utf-8")) if opts_path.is_file() else {}
        engine: Engine = SweepEngine(
            paths=paths,
            policy=pol,
            adapter=adapters["dev"],
            review_adapter=adapters["review"],
            triage_adapter=adapters["triage"],
            run_dir=run_dir,
            journal=journal,
            state=state,
            prompting=bool(opts.get("prompting", False)),
            decisions_only=bool(opts.get("decisions_only", False)),
            max_bundles=opts.get("max_bundles"),
            repeat=opts.get("repeat"),
            max_cycles=opts.get("max_cycles"),
        )
    else:
        engine = Engine(
            paths=paths,
            policy=pol,
            adapter=adapters["dev"],
            review_adapter=adapters["review"],
            run_dir=run_dir,
            journal=journal,
            state=state,
            sweep_factory=_sweep_factory(project, paths),
        )
    summary = engine.run()
    print(summary.render())
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    project = _project(args)
    run_dir = project / RUNS_DIR / args.run_id
    if not runs.is_run(run_dir):
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 1
    return _resume_paused_run(project, run_dir)


def _confirm(question: str) -> bool:
    try:
        ans = input(f"{question} [y/N] ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def cmd_resolve(args: argparse.Namespace) -> int:
    from .model import PAUSE_ESCALATION, Phase

    project = _project(args)
    run_dir = runs.run_dir_for(project, args.run_id)
    if not runs.is_run(run_dir):
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 1
    state = load_state(run_dir)
    if state.paused_stage != PAUSE_ESCALATION:
        print(
            f"run {args.run_id} is not paused at an escalation "
            f"(stage: {state.paused_stage or 'none'})",
            file=sys.stderr,
        )
        return 1
    if runs.engine_alive(run_dir):
        print(f"run {args.run_id} is still live — stop it first", file=sys.stderr)
        return 1
    story_key = args.story or state.paused_story_key
    task = state.tasks.get(story_key) if story_key else None
    if story_key is None or task is None or task.phase != Phase.ESCALATED:
        print(f"no escalated story to resolve in run {args.run_id}", file=sys.stderr)
        return 1

    if args.interactive:
        pol = policy_mod.load(_policy_path(project))
        adapters = _make_adapters(project, run_dir, pol)
        model = pol.adapter.resolved("dev").model
        resolve.build_context(state, run_dir, story_key)
        print(f"launching resolve agent for {story_key} — converse, fix the spec, then exit…")
        try:
            produced = resolve.run_session(
                adapters["dev"], project, run_dir, story_key, model=model
            )
        except NotImplementedError:
            print(
                "the dev adapter has no interactive session mode — fix the spec by hand, "
                f"then: bmad-auto resolve {args.run_id} --no-interactive",
                file=sys.stderr,
            )
            return 1
        if not produced:
            print(
                f"no resolution recorded for {story_key} " f"(agent did not write resolution.json)",
                file=sys.stderr,
            )

    # confirm-then-resume (args.resume: None = ask, True = auto, False = re-arm only)
    if args.resume is None and not _confirm(f"re-arm {story_key} and resume run {args.run_id}?"):
        print("cancelled — run is still paused at the escalation")
        return 0
    try:
        runs.rearm_escalation(run_dir, story_key)
    except runs.RearmError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"re-armed {story_key}")
    if args.resume is False:
        print(f"resume when ready: bmad-auto resume {args.run_id}")
        return 0
    from .tui import launch  # import-safe: launch.py has no textual imports

    if launch.in_ctl_session():
        # We are inside the TUI's bmad-auto-ctl window the user is attached to.
        # Tell them, hand the terminal back, and let the engine run on here — a
        # tmux pane keeps running after its client detaches.
        print(
            f"✓ resuming run {args.run_id} in the background — "
            f"watch it in the TUI, or: bmad-auto attach {args.run_id}"
        )
        launch.detach_client()
    return _resume_paused_run(project, run_dir)


def cmd_decisions(args: argparse.Namespace) -> int:
    """Answer deferred-work decisions earlier sweeps left unanswered (skipped by
    an unattended sweep, or an interactive one that was abandoned). Answers are
    recorded so the next sweep acts on them without re-asking."""
    from .sweep import DecisionPrompter

    project = _project(args)
    try:
        pending = decisions.pending_missed_decisions(project)
    except bmadconfig.BmadConfigError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    if not pending:
        print("no unanswered decisions from past sweeps")
        return 0
    if args.list:
        print(f"{len(pending)} unanswered decision(s) from past sweeps:\n")
        for d in pending:
            print(f"  {d.id}: {d.question}")
            for opt in d.options:
                rec = "  (recommended)" if opt.key == d.recommendation else ""
                print(f"      [{opt.key}] {opt.label} — {opt.effect}{rec}")
            print("")
        print("answer them interactively: bmad-auto decisions")
        return 0
    prompter = DecisionPrompter()
    print(f"{len(pending)} unanswered decision(s) — your answers carry into the next sweep:")
    today = time.strftime("%Y-%m-%d")
    for decision in pending:
        option = prompter.ask(decision)
        decisions.apply_pre_answer(project, decision, option, date=today)
        if option.effect == "close":
            outcome = "closed now"
        elif option.effect == "build":
            outcome = "queued — the next sweep will build it"
        else:
            outcome = "kept open (recorded)"
        print(f"  {decision.id}: {outcome}")
    print("\nrun `bmad-auto sweep` to act on any builds.")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project = _project(args)
    if args.run_id:
        run_dir = project / RUNS_DIR / args.run_id
    else:
        run_dir = runs.latest_run_dir(project)
    if run_dir is None or not (run_dir / "state.json").is_file():
        print("no runs found", file=sys.stderr)
        return 1
    state = load_state(run_dir)
    kind = f" [{state.run_type}]" if state.run_type != "story" else ""
    print(f"run {state.run_id}{kind}  started {state.started_at}")
    if state.finished:
        print("status: finished")
    elif state.paused:
        print(f"status: PAUSED ({state.paused_stage}) — {state.paused_reason}")
    else:
        print("status: in progress (or interrupted)")
    for key, task in state.tasks.items():
        tokens = f"{task.tokens.total:,}t" if task.tokens.total else "-"
        extra = task.defer_reason or task.commit_sha or ""
        print(
            f"  {key:40s} {task.phase:16s} dev×{task.attempt} review×{task.review_cycle} "
            f"{tokens} {extra}"
        )
    try:
        paths = bmadconfig.load_paths(project)
        ss = sprintstatus.load(paths.sprint_status)
        remaining = [s.key for s in ss.stories if s.status in sprintstatus.ACTIONABLE_STATUSES]
        print(f"sprint backlog remaining: {len(remaining)}")
    except (bmadconfig.BmadConfigError, sprintstatus.SprintStatusError):
        pass
    try:
        missed = decisions.pending_missed_decisions(project)
        if missed:
            print(
                f"deferred-work decisions awaiting an answer: {len(missed)} (bmad-auto decisions)"
            )
    except bmadconfig.BmadConfigError:
        pass
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    project = _project(args)
    run_dir = project / RUNS_DIR / args.run_id if args.run_id else runs.latest_run_dir(project)
    if run_dir is None:
        print("no runs found", file=sys.stderr)
        return 1
    return subprocess.call(runs.attach_argv(run_dir.name))


def cmd_stop(args: argparse.Namespace) -> int:
    project = _project(args)
    run_dir = runs.run_dir_for(project, args.run_id)
    if not runs.is_run(run_dir):
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 1
    if not runs.stop_run(run_dir):
        print(f"run {args.run_id} already finished", file=sys.stderr)
        return 1
    print(f"run {args.run_id} stopped")
    return 0


def cmd_delete(args: argparse.Namespace) -> int:
    project = _project(args)
    run_dir = runs.run_dir_for(project, args.run_id)
    if not runs.is_run(run_dir):
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 1
    if runs.engine_alive(run_dir):
        if not args.force:
            print(
                f"run {args.run_id} is still live — stop it first (or pass --force)",
                file=sys.stderr,
            )
            return 1
        runs.stop_run(run_dir)
    runs.delete_run(run_dir)
    print(f"run {args.run_id} deleted")
    return 0


def cmd_archive(args: argparse.Namespace) -> int:
    project = _project(args)
    run_dir = runs.run_dir_for(project, args.run_id)
    if not runs.is_run(run_dir):
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 1
    if runs.engine_alive(run_dir):
        if not args.force:
            print(
                f"run {args.run_id} is still live — stop it first (or pass --force)",
                file=sys.stderr,
            )
            return 1
        runs.stop_run(run_dir)
    dest = runs.archive_run(project, run_dir)
    print(f"run {args.run_id} archived to {dest}")
    return 0


def cmd_cleanup(args: argparse.Namespace) -> int:
    from .tui import launch  # pure stdlib; no textual import

    project = _project(args)
    prunable, live = runs.prunable_sessions(project)
    if args.dry_run:
        windows = launch.prunable_ctl_windows(project)
        if not prunable and not windows:
            print("nothing to clean up")
        else:
            for run_id in prunable:
                print(f"would kill session bmad-auto-{run_id}")
            for name in windows:
                print(f"would close ctl window {name}")
        if live:
            print(f"leaving {len(live)} live session(s) untouched")
        return 0
    killed = runs.prune_sessions(project)
    windows = launch.prune_ctl_windows(project)
    print(f"removed {len(killed)} session(s), {len(windows)} ctl window(s)")
    if live:
        print(f"left {len(live)} live session(s) untouched")
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    project = _project(args)
    try:
        from .tui.app import run_tui
    except ModuleNotFoundError as e:
        if (e.name or "").partition(".")[0] in ("textual", "tomlkit"):
            print(
                "error: the TUI requires optional dependencies — "
                "uv tool install 'bmad-automator[tui]'",
                file=sys.stderr,
            )
            return 1
        raise
    return run_tui(project)


def cmd_init(args: argparse.Namespace) -> int:
    from .install import install_into

    project = _project(args)
    if args.cli:
        clis = tuple(args.cli)
    else:
        # missing policy file yields defaults -> ("claude",)
        pol = policy_mod.load(_policy_path(project))
        clis = tuple(dict.fromkeys(pol.adapter.resolved(role).name for role in ROLES))
    return install_into(project, clis=clis, skills=args.skills, force_skills=args.force_skills)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="bmad-auto",
        description="Deterministic orchestrator for the BMAD implementation phase",
    )
    parser.add_argument("--version", action="version", version=f"bmad-auto {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name: str, func, help: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help)
        p.add_argument("--project", default=".", help="target project root (default: cwd)")
        p.set_defaults(func=func)
        return p

    init_p = add(
        "init", cmd_init, "install hooks + skills + policy template into the target project"
    )
    init_p.add_argument(
        "--cli",
        action="append",
        metavar="PROFILE",
        help="CLI profile(s) to register hooks for (claude | codex | gemini | custom; "
        "repeatable; default: profiles referenced by .automator/policy.toml, or claude)",
    )
    init_p.add_argument(
        "--no-skills",
        dest="skills",
        action="store_false",
        help="skip installing the bundled bmad-auto-* skills (hooks/policy only)",
    )
    init_p.add_argument(
        "--force-skills",
        action="store_true",
        help="overwrite bmad-auto-* skill dirs that already exist (default: skip them)",
    )
    add("validate", cmd_validate, "preflight checks; exit non-zero on failure")

    run_p = add("run", cmd_run, "run the orchestration loop")
    run_p.add_argument("--epic", type=int, help="only stories from this epic")
    run_p.add_argument("--story", help="only this story key")
    run_p.add_argument("--max-stories", type=int, help="stop after N stories")
    run_p.add_argument("--dry-run", action="store_true", help="print the plan, spawn nothing")
    run_p.add_argument("--run-id", help=argparse.SUPPRESS)  # pre-assigned id (used by the TUI)

    sweep_p = add("sweep", cmd_sweep, "triage + execute open deferred-work.md entries")
    sweep_p.add_argument(
        "--no-prompt",
        action="store_true",
        help="unattended: skip decision prompts, run only decision-free bundles",
    )
    sweep_p.add_argument(
        "--decisions-only",
        action="store_true",
        help="triage + answer decisions + record them; run no bundles",
    )
    sweep_p.add_argument("--max-bundles", type=int, help="override [sweep] max_bundles")
    sweep_p.add_argument(
        "--repeat",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="override [sweep] repeat: after a cycle completes, re-triage and continue "
        "on newly deferred work until nothing addressable completes",
    )
    sweep_p.add_argument("--max-cycles", type=int, help="override [sweep] max_cycles")
    sweep_p.add_argument(
        "--dry-run", action="store_true", help="list open ledger entries, spawn nothing"
    )
    sweep_p.add_argument("--run-id", help=argparse.SUPPRESS)  # pre-assigned id (used by the TUI)

    resume_p = add("resume", cmd_resume, "resume a paused run")
    resume_p.add_argument("run_id")

    resolve_p = add(
        "resolve", cmd_resolve, "resolve a CRITICAL escalation interactively, then re-arm + resume"
    )
    resolve_p.add_argument("run_id")
    resolve_p.add_argument("--story", help="story key to resolve (default: the paused one)")
    resolve_p.add_argument(
        "--no-interactive",
        dest="interactive",
        action="store_false",
        help="skip the resolve agent (spec already fixed by hand); just re-arm + resume",
    )
    resolve_p.add_argument(
        "--resume",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="--resume: re-arm + resume without prompting; --no-resume: re-arm only "
        "(default: prompt to confirm, then resume)",
    )

    decisions_p = add(
        "decisions",
        cmd_decisions,
        "answer deferred-work decisions earlier sweeps left unanswered",
    )
    decisions_p.add_argument(
        "--list",
        action="store_true",
        help="list the pending decisions without answering them",
    )

    status_p = add("status", cmd_status, "show run + sprint state")
    status_p.add_argument("run_id", nargs="?")

    attach_p = add("attach", cmd_attach, "tmux attach to a run's session")
    attach_p.add_argument("run_id", nargs="?")

    stop_p = add("stop", cmd_stop, "stop a live run (engine + agent session)")
    stop_p.add_argument("run_id")

    delete_p = add("delete", cmd_delete, "delete a run directory")
    delete_p.add_argument("run_id")
    delete_p.add_argument(
        "--force", action="store_true", help="stop the run first if it is still live"
    )

    archive_p = add("archive", cmd_archive, "compress a run into .automator/archive and remove it")
    archive_p.add_argument("run_id")
    archive_p.add_argument(
        "--force", action="store_true", help="stop the run first if it is still live"
    )

    cleanup_p = add(
        "cleanup", cmd_cleanup, "remove tmux sessions/windows for finished or stopped runs"
    )
    cleanup_p.add_argument(
        "--dry-run",
        action="store_true",
        help="list what would be removed without killing anything",
    )

    add(
        "tui",
        cmd_tui,
        "interactive dashboard (needs `uv tool install 'bmad-automator[tui]'`)",
    )

    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (
        bmadconfig.BmadConfigError,
        sprintstatus.SprintStatusError,
        policy_mod.PolicyError,
        verify.GitError,
    ) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
