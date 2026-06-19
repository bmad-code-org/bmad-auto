#!/usr/bin/env python3
"""per_worktree setup for the bmad-auto Unity engine plugin.

Runs once per unit, right after bmad-auto cuts the unit's git worktree and before
the readiness gate, to turn that fresh checkout into a usable Unity project with
its own managed Editor:

  1. Give the worktree a *warm* ``Library`` by reflink/CoW-copying the project's
     main ``Library`` into it (NOT the operator's live Library directly, and NOT a
     symlink to it — sharing one Library across two Editors corrupts it). A fresh
     worktree has no Library (it is gitignored, so never checked out); opening Unity
     on an empty Library forces a *cold full reimport* of the whole project, which on
     a real project melts the import workers and SIGFPEs Burst mid-artifact-write
     (the "Opening file VirtualArtifacts/Primary/<hash>" crash). Seeding a warm
     Library makes it an *incremental* reimport of just the changed assets instead.
     On a CoW filesystem (btrfs/xfs) the copy is near-instant and shares extents, so
     it costs almost no time or disk; elsewhere it falls back to a deep copy, then to
     a symlinked empty per-worktree cache (the old behavior) if no warm source exists.
  2. Write the worktree's MCP client config (``.mcp.json``) via ``setup-mcp`` and
     pin the *Unity project* to local ("Custom") mode via ``bootstrap-local``. The
     IvanMurzak CLI derives a deterministic MCP port from the *project path*, so a
     worktree at a different path automatically gets its own port and self-isolates
     from the operator's main Editor with no manual port wiring.
  3. Launch a Unity Editor on the worktree path (detached) **with local-connection
     env** so it connects to that per-path local server and *hosts the server
     itself* (``--start-server true``) rather than the cloud. The plugin's
     ``ready_cmd`` (the engine's readiness gate) then blocks until that Editor +
     server are up, so this script only needs to start it, not wait for it.

Why custom/local mode matters: a bare ``open`` passes no MCP connection env, so the
Editor falls back to its persisted config (cloud by default) — the worktree Editor
then isn't talking to its per-worktree server at all. And without ``--start-server
true`` the local server only exists once an MCP *client* (the agent) spawns it, so
the readiness gate can't observe it before the agent runs. Pinning Custom mode +
Editor-hosted server fixes both: the Editor connects locally and readiness becomes
observable without any client. (A loopback ``--url`` is what flips the Editor off
cloud; ``bootstrap-local`` persists ``connectionMode: Custom`` in the project's
``UserSettings/AI-Game-Developer-Config.json`` so the Editor UI shows it too.)

The MCP tool *skill* files are not written here — they are gitignored and copied
in from the main repo by the plugin's ``seed_globs`` (``.claude/skills/*``).

Verified against unity-mcp-cli v0.81.0 (`setup-mcp` writes ``.mcp.json`` with the
deterministic local URL; `bootstrap-local --url --token` pins Custom mode; `open`
injects UNITY_MCP_* env only when connection options are passed). The exact flags
move between releases — override engine.worktree_setup_cmd in a project-local
plugin if yours differ.

Only the IvanMurzak MCP is wired for a managed per-worktree launch. CoplayDev runs
one shared :8080 server multiplexing Editors by instance id, so its per-worktree
story differs — point engine.worktree_setup_cmd at your own script for it.

Env (injected by the engine, all optional except the worktree):
  BMAD_AUTO_WORKTREE         the unit's worktree (the Unity project to manage)
  BMAD_AUTO_REPO_ROOT        main repo root (parent of the Library cache)
  BMAD_AUTO_ENGINE_MCP       ivanmurzak | coplaydev            (default ivanmurzak)
  BMAD_AUTO_UNITY_PATH       explicit Editor binary            (skips Unity Hub discovery)
  BMAD_AUTO_ENGINE_AGENT     agent id for setup-mcp            (default claude-code)
  BMAD_AUTO_UNITY_LIBRARY_CACHE  override the symlink-fallback Library cache root
  BMAD_AUTO_UNITY_LIBRARY_SEED   warm Library to prime from   (default <repo>/Library;
                                 empty string disables priming → symlink fallback)
  BMAD_AUTO_UNITY_LIBRARY_SEED_MODE  reflink | copy | symlink | off   (default reflink)
  UNITY_MCP_CLI              IvanMurzak CLI binary             (default unity-mcp-cli)

Local-connection knobs (defaults reproduce the recommended Custom/local launch):
  BMAD_AUTO_UNITY_MCP_LOCAL          1/true to pin Custom mode (default); 0/false
                                     reverts to a bare cloud-config ``open``
  BMAD_AUTO_UNITY_MCP_URL            local server URL (default: read from .mcp.json)
  BMAD_AUTO_UNITY_MCP_TOKEN          bearer token (default empty — auth none)
  BMAD_AUTO_UNITY_MCP_TRANSPORT      streamableHttp | stdio   (default streamableHttp)
  BMAD_AUTO_UNITY_MCP_AUTH           none | required          (default none)
  BMAD_AUTO_UNITY_MCP_START_SERVER   true | false             (default true)
  BMAD_AUTO_UNITY_MCP_KEEP_CONNECTED true | false             (default true)

Exit 0 = the worktree Editor is launching; non-zero = setup failed (the engine
defers the unit and notifies).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

# how long to watch a freshly-launched Editor for an immediate crash before
# treating "still running" as a successful launch (the ready gate does the wait).
_LAUNCH_GRACE_SEC = 15
# the MCP server name setup-mcp writes into .mcp.json (IvanMurzak's agent config).
_MCP_SERVER_NAME = "ai-game-developer"


def _truthy(value: str | None, default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _worktree() -> Path | None:
    wt = os.environ.get("BMAD_AUTO_WORKTREE")
    return Path(wt) if wt else None


def _cli() -> str:
    return os.environ.get("UNITY_MCP_CLI", "unity-mcp-cli")


def _library_cache(worktree: Path) -> Path:
    """A persistent, per-worktree Library cache dir (keyed by worktree name).

    Lives under the repo's gitignored .automator/cache/ (init adds the ignore);
    relocate with BMAD_AUTO_UNITY_LIBRARY_CACHE (e.g. onto a faster disk)."""
    override = os.environ.get("BMAD_AUTO_UNITY_LIBRARY_CACHE")
    if override:
        root = Path(override)
    else:
        repo = Path(os.environ.get("BMAD_AUTO_REPO_ROOT", worktree.parent))
        root = repo / ".automator" / "cache" / "unity" / "Library"
    return root / worktree.name


# Top-level Library entries a primed copy must NOT carry into the worktree:
# the per-Editor identity file (would make the worktree Editor think another
# instance owns the project) plus locks/pids. Globs (*-lock, *.pid) are also
# stripped. The MCP server binary under mcp-server/ is intentionally kept (it is
# expensive to re-extract and is launched by port arg, not a stale on-disk config).
_LIBRARY_VOLATILE = ("EditorInstance.json",)


def _seed_source() -> Path | None:
    """The warm Library to prime a worktree from. Defaults to ``<repo>/Library``
    (the operator's main project Library); ``BMAD_AUTO_UNITY_LIBRARY_SEED`` overrides
    it, and an explicit empty value disables priming. Returns None if no non-empty
    source is available (the caller then falls back to a symlinked empty cache)."""
    override = os.environ.get("BMAD_AUTO_UNITY_LIBRARY_SEED")
    if override is not None:
        override = override.strip()
        if not override:
            return None  # explicitly disabled
        src = Path(override)
    else:
        repo = os.environ.get("BMAD_AUTO_REPO_ROOT", "").strip()
        if not repo:
            return None
        src = Path(repo) / "Library"
    try:
        return src if src.is_dir() and any(src.iterdir()) else None
    except OSError:
        return None


def _copy_library(src: Path, dest: Path, *, reflink: bool) -> bool:
    """Reflink (CoW) or deep-copy a warm Library into the worktree so Unity does an
    incremental — not cold — import. ``reflink=auto`` is ~free on btrfs/xfs and
    silently deep-copies elsewhere. Strips per-Editor identity/lock/pid files the
    copy must not carry. Returns True on success (a partial copy is cleaned up)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    reflink_arg = "--reflink=auto" if reflink else "--reflink=never"
    # `cp -a src/. dest` copies the tree's contents (incl. dotfiles) into dest,
    # creating dest if absent; -a preserves perms/timestamps so caches stay valid.
    proc = subprocess.run(
        ["cp", "-a", reflink_arg, f"{src}/.", str(dest)],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        kind = "reflink" if reflink else "copy"
        print(f"unity_setup: Library prime ({kind}) failed; cleaning up", file=sys.stderr)
        shutil.rmtree(dest, ignore_errors=True)
        return False
    for victim in (
        [dest / name for name in _LIBRARY_VOLATILE]
        + list(dest.glob("*-lock"))
        + list(dest.glob("*.pid"))
    ):
        if victim.is_dir() and not victim.is_symlink():
            shutil.rmtree(victim, ignore_errors=True)
        else:
            try:
                victim.unlink()
            except OSError:
                pass
    print(
        f"unity_setup: Library primed from {src} ({'reflink' if reflink else 'copy'})",
        file=sys.stderr,
    )
    return True


def _link_library_cache(worktree: Path) -> None:
    """Fallback: point <worktree>/Library at an (initially empty) per-worktree cache
    via symlink. Used only when priming is off or no warm source is available — the
    first run is then cold (slow, and on a big project crash-prone), so priming is
    preferred. The cache survives across runs to amortize re-runs of the same unit."""
    link = worktree / "Library"
    cache = _library_cache(worktree)
    cache.mkdir(parents=True, exist_ok=True)
    link.symlink_to(cache, target_is_directory=True)
    print(f"unity_setup: Library -> {cache} (symlink; cold first import)", file=sys.stderr)


def _prime_library(worktree: Path) -> None:
    """Give the worktree a warm Library so Unity imports incrementally, not cold.

    Leaves an already-populated real Library untouched (a lone ``ScriptAssemblies``
    dir counts as empty — a cold leftover — so we still prime). Drops a stale symlink
    from the old symlink-mode setup. Then reflink/copies the warm seed Library in, or
    falls back to a symlinked empty cache when priming is off or no seed exists."""
    link = worktree / "Library"
    if link.is_symlink():
        link.unlink()  # stale symlink from the old symlink-mode setup
    elif link.exists():
        try:
            substantive = {p.name for p in link.iterdir()} - {"ScriptAssemblies"}
        except OSError:
            substantive = {"?"}  # unreadable — assume real, don't clobber
        if substantive:
            return  # a genuine Library is in place — never clobber the operator's tree
        shutil.rmtree(link, ignore_errors=True)  # cold leftover (ScriptAssemblies only)

    mode = os.environ.get("BMAD_AUTO_UNITY_LIBRARY_SEED_MODE", "reflink").strip().lower()
    if mode != "off" and mode != "symlink":
        src = _seed_source()
        if src is not None and _copy_library(src, link, reflink=(mode != "copy")):
            return
        if src is None:
            print(
                "unity_setup: no warm Library to prime from "
                "(set BMAD_AUTO_UNITY_LIBRARY_SEED or run the main Editor once); "
                "falling back to a symlinked empty cache — first import will be cold",
                file=sys.stderr,
            )
    _link_library_cache(worktree)


def _local_url(worktree: Path) -> str | None:
    """The worktree's local MCP server URL: an explicit override, else the URL
    setup-mcp wrote into ``<worktree>/.mcp.json`` (its deterministic per-path
    port). Returns None if neither is available — the caller then opens in the
    project's persisted (cloud) mode rather than guessing a port."""
    override = os.environ.get("BMAD_AUTO_UNITY_MCP_URL")
    if override and override.strip():
        return override.strip()
    cfg = worktree / ".mcp.json"
    try:
        data = json.loads(cfg.read_text())
        servers = data.get("mcpServers", {})
        entry = servers.get(_MCP_SERVER_NAME) or next(iter(servers.values()), {})
        url = entry.get("url")
        return url.strip() if isinstance(url, str) and url.strip() else None
    except (OSError, ValueError, AttributeError):
        return None


def _bootstrap_local(cli: str, worktree: Path, url: str) -> int:
    """Pin the Unity project to local ("Custom") mode so its Editor connects to
    the per-worktree server, not the cloud. Idempotent; --token is mandatory but
    unused under auth=none (the agent's .mcp.json is tokenless)."""
    token = os.environ.get("BMAD_AUTO_UNITY_MCP_TOKEN", "")
    proc = subprocess.run(
        [cli, "bootstrap-local", str(worktree), "--url", url, "--token", token],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout + proc.stderr)
        print(
            "unity_setup: bootstrap-local failed (Editor may open in cloud mode)", file=sys.stderr
        )
    return proc.returncode


def _open_command(cli: str, worktree: Path, url: str | None) -> list[str]:
    """The ``open`` argv. With a local URL we pass connection options so the
    Editor opens in Custom mode and hosts the server itself; without one we fall
    back to a bare open (the project's persisted/cloud config)."""
    cmd = [cli, "open", str(worktree)]
    if url is not None and _truthy(os.environ.get("BMAD_AUTO_UNITY_MCP_LOCAL"), True):
        transport = os.environ.get("BMAD_AUTO_UNITY_MCP_TRANSPORT", "streamableHttp")
        auth = os.environ.get("BMAD_AUTO_UNITY_MCP_AUTH", "none")
        start_server = (
            "true" if _truthy(os.environ.get("BMAD_AUTO_UNITY_MCP_START_SERVER"), True) else "false"
        )
        cmd += [
            "--url",
            url,  # a loopback URL flips the Editor off cloud → Custom mode
            "--transport",
            transport,
            "--auth",
            auth,
            "--start-server",
            start_server,  # Editor hosts the server (client-independent readiness)
        ]
        token = os.environ.get("BMAD_AUTO_UNITY_MCP_TOKEN", "")
        if token:
            cmd += ["--token", token]
        if _truthy(os.environ.get("BMAD_AUTO_UNITY_MCP_KEEP_CONNECTED"), True):
            cmd += ["--keep-connected"]  # bare flag: hold the bridge open before/after the client
    editor = os.environ.get("BMAD_AUTO_UNITY_PATH")
    if editor:
        cmd += ["--editor-path", editor]
    return cmd


def _setup_ivanmurzak(worktree: Path) -> int:
    cli = _cli()
    if shutil.which(cli) is None:
        print(
            f"unity_setup: {cli!r} not found on PATH; install the Unity-MCP CLI, set "
            "UNITY_MCP_CLI, or override engine.worktree_setup_cmd",
            file=sys.stderr,
        )
        return 2

    # 1. worktree MCP client config (deterministic per-path port; no Editor needed)
    agent = os.environ.get("BMAD_AUTO_ENGINE_AGENT", "claude-code")
    cfg = subprocess.run(
        [cli, "setup-mcp", agent, str(worktree)],
        capture_output=True,
        text=True,
    )
    if cfg.returncode != 0:
        sys.stderr.write(cfg.stdout + cfg.stderr)
        print("unity_setup: setup-mcp failed", file=sys.stderr)
        return cfg.returncode

    # 2. pin the project to local/Custom mode (so the Editor connects to the
    #    per-worktree server, not the cloud). Best effort: a failure here just
    #    means the Editor may open in cloud mode — the open below still passes the
    #    local --url, which is what actually flips it off cloud for this session.
    url = _local_url(worktree)
    if url is not None and _truthy(os.environ.get("BMAD_AUTO_UNITY_MCP_LOCAL"), True):
        _bootstrap_local(cli, worktree, url)
    elif url is None:
        print(
            "unity_setup: could not derive a local MCP URL (no .mcp.json url / "
            "BMAD_AUTO_UNITY_MCP_URL); opening in the project's persisted mode",
            file=sys.stderr,
        )

    # 3. launch the worktree's Editor, detached, and watch briefly for a crash.
    cmd = _open_command(cli, worktree, url)
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,  # detach so it outlives this hook
        )
    except OSError as exc:
        print(f"unity_setup: could not launch Editor: {exc}", file=sys.stderr)
        return 1
    deadline = time.monotonic() + _LAUNCH_GRACE_SEC
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is not None:
            # `open` either daemonizes the Editor and exits 0 (fine), or failed.
            if rc != 0:
                print(f"unity_setup: 'open' exited {rc} during launch", file=sys.stderr)
            return rc
        time.sleep(1)
    # still running after the grace window: an attached launch we leave detached;
    # the readiness gate confirms the Editor + MCP actually came up.
    print("unity_setup: Editor launching (readiness gate will confirm)", file=sys.stderr)
    return 0


def main() -> int:
    worktree = _worktree()
    if worktree is None:
        print("unity_setup: BMAD_AUTO_WORKTREE is not set", file=sys.stderr)
        return 2
    mcp = (os.environ.get("BMAD_AUTO_ENGINE_MCP") or "ivanmurzak").strip().lower()
    _prime_library(worktree)
    if mcp == "ivanmurzak":
        return _setup_ivanmurzak(worktree)
    if mcp == "coplaydev":
        print(
            "unity_setup: per_worktree managed-launch is not wired for the CoplayDev "
            "MCP (one shared :8080 server multiplexes Editors). Override "
            "engine.worktree_setup_cmd with a CoplayDev launcher, or use editor_mode "
            "= 'shared'.",
            file=sys.stderr,
        )
        return 2
    print(
        f"unity_setup: unknown BMAD_AUTO_ENGINE_MCP={mcp!r} (expected ivanmurzak|coplaydev)",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
