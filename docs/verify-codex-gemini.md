# Operator guide: verifying the Codex & Gemini adapters

The profile system is implemented and unit/integration tested, but three
things can only be confirmed by a human with real accounts — spawned sessions
cannot answer first-run dialogs, and both CLIs need your auth:

1. **Codex prompt phrasing** — which exact wording reliably attaches the
   `$bmad-quick-dev` skill from the initial prompt argument (Codex does not
   expand `/commands` there).
2. **Codex env pass-through** — that hook subprocesses see the `BMAD_AUTO_*`
   variables (its `shell_environment_policy` filters env for spawned
   processes; our vars shouldn't match the filter, but verify).
3. **Gemini slash resolution + transcript schema** — whether skills are
   exposed as `/bmad-quick-dev` from `-i` (if not, a one-file shim fixes it),
   and a transcript snapshot so the token-usage parser can be finalized.

Total time: ~30 minutes. Everything runs in the existing sandbox at
`/tmp/bmad-e2e` (greeter project, baseline commit `be65537` = before story
1-1 was implemented). Story `1-1-add-farewell-support` is deliberately tiny.

---

## Step 0 — versions and auth (once)

```bash
codex --version    # need >= 0.139
gemini --version   # need >= 0.46
```

Update via your usual install channel if below the floor. Then authenticate
each once (anywhere): `codex login`; for Gemini either run `gemini` once and
complete the browser OAuth, or `export GEMINI_API_KEY=...`.

## Step 1 — reset the sandbox and register hooks

```bash
cd /tmp/bmad-e2e
git reset --hard be65537 && git clean -fd   # back to pre-story baseline
bmad-auto init --cli codex --cli gemini     # writes .codex/hooks.json + .gemini/settings.json
git add -A && git commit -m "sandbox: codex+gemini adapter configs"
git rev-parse --short HEAD                  # note this sha — it's your CONFIG_BASE
```

The commit matters: `bmad-auto run` requires a **clean worktree** (the
orchestrator rolls back to a git baseline between dev attempts, so anything
uncommitted — including policy.toml edits — would block or be lost). Any
config change before a run must be committed.

"Fresh sandbox" below means: `git reset --hard <CONFIG_BASE> && git clean -fd`.

**Never reset to `be65537` after this point** — that commit predates the
adapter configs, so resetting to it wipes `.codex/`/`.gemini/` (hooks
silently gone → no events → instant "crashed") and resurrects the old
policy.toml whose `extra_args` line crashes codex/gemini at launch (this
exact combination produced deferred run 20260611-101521-a74e). If it
happens: re-run `bmad-auto init --cli codex --cli gemini`, delete the
`extra_args` line, commit, and treat that commit as the new CONFIG_BASE.

## Step 2 — first-run trust, per CLI (inside the sandbox)

- `codex` → accept the workspace-trust prompt → a **second prompt appears for
  hooks** ("Hooks need review … Hooks can run outside the sandbox") — choose
  **"Trust all and continue"** ("continue without trusting" silently disables
  hooks: the orchestrator would see no events at all) → type `/skills` and
  confirm `bmad-quick-dev` and `bmad-code-review` are listed → quit.
  Note: the hooks prompt reappears whenever the hook config changes, e.g.
  after re-running `bmad-auto init` in a new project.
- `gemini` → complete any auth/trust prompt → type `/` and **note whether
  `bmad-quick-dev` appears in the command list** → quit.
  *Record YES or NO — this decides Step 3c.*

## Step 3 — pre-flight checks (no orchestrator yet)

### 3a. Hook wiring + env pass-through

```bash
cd /tmp/bmad-e2e
env BMAD_AUTO_RUN_DIR=/tmp/hooktest-codex BMAD_AUTO_TASK_ID=smoke-1 \
    codex "Say hi and end your turn."
# let it answer, then quit, then:
ls /tmp/hooktest-codex/events/
```

**Expected:** two files, `…-smoke-1-SessionStart.json` and
`…-smoke-1-Stop.json`. Empty/missing directory = hooks not firing or env
stripped — stop and report.

Same for Gemini:

```bash
env BMAD_AUTO_RUN_DIR=/tmp/hooktest-gemini BMAD_AUTO_TASK_ID=smoke-1 \
    gemini -i "Say hi and end your turn." --approval-mode=yolo
ls /tmp/hooktest-gemini/events/
cat /tmp/hooktest-gemini/events/*Stop*    # note the transcript_path value
```

### 3b. Codex skill-mention phrasing

Fresh sandbox, then run `codex` interactively and paste exactly:

```
Use the $bmad-quick-dev skill now: 1-1-add-farewell-support
```

Watch whether the skill actually **attaches** (skill banner / it starts
following the workflow steps) vs. the model just freestyling. Quit as soon as
attachment is clear — don't let it finish. If it did NOT attach, try in order
and note which one works:

1. `$bmad-quick-dev 1-1-add-farewell-support`
2. `Run the bmad-quick-dev skill for story 1-1-add-farewell-support`

The winning phrasing becomes `prompt_template` in
`src/automator/data/profiles/codex.toml`.

### 3c. Gemini slash resolution

Fresh sandbox, then:

```bash
gemini -i "/bmad-quick-dev 1-1-add-farewell-support" --approval-mode=yolo
```

If it starts executing the skill → done, quit. If it says unknown command or
treats it as plain text, install the shim and retest:

```toml
# /tmp/bmad-e2e/.gemini/commands/bmad-quick-dev.toml
description = "bmad-auto shim: run the bmad-quick-dev skill"
prompt = "Use the bmad-quick-dev agent skill to implement this story, following its automation-mode instructions exactly: {{args}}"
```

(If the shim is needed, say so — shim generation will be added to
`bmad-auto init --cli gemini` for both skills.)

## Step 4 — full orchestrated run, per CLI

Fresh sandbox; set the adapter in `/tmp/bmad-e2e/.automator/policy.toml`:

```toml
[adapter]
name = "codex"        # then repeat the whole step with "gemini"
```

**If the policy.toml predates the profile system** (old template), also delete
any `extra_args = ["--permission-mode", "bypassPermissions"]` line — when set,
`extra_args` *replaces* the profile's bypass flags, and Claude's flags crash
codex/gemini at launch. Leave it unset so each profile supplies its own.

Commit it (clean-worktree requirement):

```bash
git commit -am "sandbox: adapter -> codex"
```

Then:

```bash
bmad-auto validate    # all green expected
bmad-auto run --story 1-1-add-farewell-support
```

`run` blocks its terminal until the story finishes (deterministic foreground
loop) — monitor from a **second shell**:

```bash
bmad-auto attach      # watch the live session; from inside tmux this
                      # switches your client (tmux switch-client -l to return)
bmad-auto status      # phase / attempt / token summary, read-only
tail -f .automator/runs/<run-id>/journal.jsonl   # every orchestrator decision
```

A dev session can take several minutes with no orchestrator output — that's
normal; the journal and the attached pane show it working.

**Success looks like:** run summary reports the story done, `git log -1`
shows the orchestrator's story commit, `bmad-auto status` shows token usage
for codex (gemini may show `-` until the parser is finalized — that's what
the snapshot below is for).

(The Gemini transcript schema was captured live on 2026-06-11 and the usage
parser finalized against it — no snapshot needed; `bmad-auto status` should
show real token counts for both CLIs.)

## Step 5 — what to report back

- 3a: contents of both `events/` dirs (or "empty" + which CLI)
- 3b: which Codex phrasing attached the skill
- 3c: whether Gemini resolved `/bmad-quick-dev` natively or needed the shim
- Step 4: final run summary line per CLI, and the token counts from `bmad-auto status`
- If anything failed: the run's `journal.jsonl` and `logs/<task-id>.log` from
  `.automator/runs/<run-id>/`

## Failure signatures

| Symptom | Likely cause |
|---|---|
| run ends `timeout`, attached window shows a dialog | first-run trust/auth not done (Step 2) |
| `stalled` after a Stop event | skill ran but never wrote result.json → skill wasn't actually invoked (phrasing/shim) or ignored automation mode; check `tasks/<id>/` and the pane log |
| `crashed` immediately | bad binary/flag for that CLI version; check `logs/<task-id>.log` — the CLI's help text in the log means a rejected flag, almost always a stale `extra_args` in policy.toml |
| no event files, window alive and chatting | hooks not registered (`bmad-auto validate`), not trusted (Codex "Hooks need review" prompt, Step 2), or env vars stripped (3a) |
