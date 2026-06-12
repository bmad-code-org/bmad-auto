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
    deferredwork,
)
from . import policy as policy_mod
from . import (
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


def cmd_resume(args: argparse.Namespace) -> int:
    project = _project(args)
    paths = bmadconfig.load_paths(project)
    run_dir = project / RUNS_DIR / args.run_id
    if not (run_dir / "state.json").is_file():
        print(f"no such run: {args.run_id}", file=sys.stderr)
        return 1
    state = load_state(run_dir)
    if state.finished:
        print(f"run {args.run_id} already finished", file=sys.stderr)
        return 1
    pol = policy_mod.load(_policy_path(project))
    journal = Journal(run_dir)
    journal.append("run-resume", was_paused=state.paused_reason)
    state.clear_pause()
    runs.write_pid(run_dir)
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
    return 0


def cmd_attach(args: argparse.Namespace) -> int:
    project = _project(args)
    run_dir = project / RUNS_DIR / args.run_id if args.run_id else runs.latest_run_dir(project)
    if run_dir is None:
        print("no runs found", file=sys.stderr)
        return 1
    return subprocess.call(runs.attach_argv(run_dir.name))


def cmd_tui(args: argparse.Namespace) -> int:
    project = _project(args)
    try:
        from .tui.app import run_tui
    except ModuleNotFoundError as e:
        if (e.name or "").partition(".")[0] in ("textual", "tomlkit"):
            print(
                "error: the TUI requires optional dependencies — "
                "pip install 'bmad-automator[tui]'",
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
    return install_into(project, clis=clis)


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

    init_p = add("init", cmd_init, "install hooks + policy template into the target project")
    init_p.add_argument(
        "--cli",
        action="append",
        metavar="PROFILE",
        help="CLI profile(s) to register hooks for (claude | codex | gemini | custom; "
        "repeatable; default: profiles referenced by .automator/policy.toml, or claude)",
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
        "--dry-run", action="store_true", help="list open ledger entries, spawn nothing"
    )
    sweep_p.add_argument("--run-id", help=argparse.SUPPRESS)  # pre-assigned id (used by the TUI)

    resume_p = add("resume", cmd_resume, "resume a paused run")
    resume_p.add_argument("run_id")

    status_p = add("status", cmd_status, "show run + sprint state")
    status_p.add_argument("run_id", nargs="?")

    attach_p = add("attach", cmd_attach, "tmux attach to a run's session")
    attach_p.add_argument("run_id", nargs="?")

    add(
        "tui",
        cmd_tui,
        "interactive dashboard (needs `pip install 'bmad-automator[tui]'`)",
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
