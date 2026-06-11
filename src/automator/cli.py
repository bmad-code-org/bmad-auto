"""bmad-auto command line interface."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

from . import __version__, bmadconfig, policy as policy_mod, sprintstatus, verify
from .adapters.base import CodingCLIAdapter
from .engine import Engine
from .journal import Journal, load_state, save_state
from .model import RunState

RUNS_DIR = Path(".automator") / "runs"
POLICY_FILE = Path(".automator") / "policy.toml"


def _project(args: argparse.Namespace) -> Path:
    return Path(args.project).resolve()


def _policy_path(project: Path) -> Path:
    return project / POLICY_FILE


ROLES = ("dev", "review")


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
                run_dir=run_dir, policy=policy, profile=profile, extra_args=cfg.extra_args
            )
        adapters[role] = by_cfg[cfg]
    return adapters


def _new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def _latest_run_dir(project: Path) -> Path | None:
    runs = project / RUNS_DIR
    if not runs.is_dir():
        return None
    candidates = sorted(d for d in runs.iterdir() if (d / "state.json").is_file())
    return candidates[-1] if candidates else None


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
            f"adapter dev={role_names['dev']}, review={role_names['review']}"
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

    run_id = _new_run_id()
    run_dir = project / RUNS_DIR / run_id
    journal = Journal(run_dir)
    state = RunState(
        run_id=run_id,
        project=str(project),
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        policy_snapshot=pol.to_dict(),
    )
    save_state(run_dir, state)
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
    )
    summary = engine.run()
    print(summary.render())
    return 0


def _dry_run(paths: bmadconfig.ProjectPaths, pol, args: argparse.Namespace) -> int:
    from .adapters.profile import get_profile

    def render(role: str, prompt: str) -> str:
        cfg = pol.adapter.resolved(role)
        profile = get_profile(cfg.name, paths.project)
        extra = cfg.extra_args if cfg.extra_args is not None else profile.bypass_args
        argv = [profile.binary, *profile.launch_args, f'"{profile.render_prompt(prompt)}"', *extra]
        if cfg.model:
            argv += [profile.model_flag, cfg.model]
        return " ".join(argv)

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
        print(f"    dev:    {render('dev', f'/bmad-quick-dev {story.key}')}")
        print(f"    review: {render('review', '/bmad-code-review <spec from dev>')}")
        print(f"    env:    BMAD_AUTO_MODE=1 BMAD_AUTO_STORY_KEY={story.key}")
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
    adapters = _make_adapters(project, run_dir, pol)
    engine = Engine(
        paths=paths,
        policy=pol,
        adapter=adapters["dev"],
        review_adapter=adapters["review"],
        run_dir=run_dir,
        journal=journal,
        state=state,
    )
    summary = engine.run()
    print(summary.render())
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    project = _project(args)
    if args.run_id:
        run_dir = project / RUNS_DIR / args.run_id
    else:
        run_dir = _latest_run_dir(project)
    if run_dir is None or not (run_dir / "state.json").is_file():
        print("no runs found", file=sys.stderr)
        return 1
    state = load_state(run_dir)
    print(f"run {state.run_id}  started {state.started_at}")
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
    run_dir = (
        project / RUNS_DIR / args.run_id if args.run_id else _latest_run_dir(project)
    )
    if run_dir is None:
        print("no runs found", file=sys.stderr)
        return 1
    session = f"bmad-auto-{run_dir.name}"
    if os.environ.get("TMUX"):
        # already inside tmux: nesting is refused, switch this client instead
        # (tmux switch-client -l comes back)
        return subprocess.call(["tmux", "switch-client", "-t", f"={session}"])
    return subprocess.call(["tmux", "attach", "-t", session])


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

    resume_p = add("resume", cmd_resume, "resume a paused run")
    resume_p.add_argument("run_id")

    status_p = add("status", cmd_status, "show run + sprint state")
    status_p.add_argument("run_id", nargs="?")

    attach_p = add("attach", cmd_attach, "tmux attach to a run's session")
    attach_p.add_argument("run_id", nargs="?")

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
