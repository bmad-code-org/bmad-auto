# Finalizing a CLI adapter profile with `probe-adapter`

bmad-auto drives any coding CLI that fits the **tmux-injection + hook-signal**
transport through one generic adapter (`adapters/generic_tmux.py`); everything
CLI-specific lives in a declarative **TOML profile** (`adapters/profile.py`). The
[README adapter section](../README.md#other-coding-clis) covers the profile fields
and how to drop one in without touching Python.

The hard part of a new profile isn't the TOML — it's the **facts that live in no
doc**: the CLI's exact hook payload shape (field names and casing, whether
`session_id` / `transcript_path` / `cwd` are present), where it writes its session
transcript and in what format, and the token-usage schema a `usage_parser` has to
read. Historically the only way to get these was to hand a volunteer a manual
recipe and ask them to sanitize the output by hand — error-prone and PII-risky.

**`bmad-auto probe-adapter`** (alias `collect-adapter-data`) pulls all of that and
runs it through an audited sanitizer, so a user of any coding CLI can run one
command and paste back a clean, content-free report.

```bash
bmad-auto probe-adapter <cli> --project .          # default: zero-launch scan
bmad-auto probe-adapter <cli> --probe --project .  # opt-in live capture
```

---

## Two modes

Both modes emit the **same single sanitized report** (markdown to stdout, or to a
file with `--out`; add `--json` for a machine-readable block).

### SCAN (default — no process launch)

Runs `<binary> --version` / `--help`, locates the newest **already-existing**
session transcript by convention, reads the declared hook config, and infers the
token schema from the transcript. Works whenever you've used the CLI before, with
zero execution risk. This is the right first step for any CLI that already has a
profile (claude/codex/gemini/copilot) or that you've run by hand.

### PROBE (`--probe` — opt-in live capture)

In an ephemeral `mkdtemp` workspace, `probe` registers a full-payload capture hook
for every native event in the profile, launches **one trivial content-free turn**
(`Reply with exactly: OK`) in a tmux window, captures each hook event's complete
payload, locates the transcript, then tears everything down. Use it to confirm the
**exact hook payload shape** and that the CLI actually **accepts the hook dialect**
your profile declares — facts scan can't see without running the CLI.

`--probe` needs a known profile (it uses the profile's hook dialect and event map).
If `tmux` or the binary is missing, probe degrades gracefully to a scan.

---

## PII safety model

The report is built to be **safe to paste into an issue or PR**. A single audited
sanitizer (`src/automator/sanitize.py`) is the only chokepoint:

- **numbers, booleans, and `null` pass through** — token _counts_ are not PII;
- **dict keys are kept verbatim** — field names and casing are the whole point of
  a payload probe;
- every **leaf string** is `$HOME`→`~` redacted and then kept **only if** it looks
  like a short machine identifier (e.g. `claude-opus-4-8`, `session-abc_123`);
  anything else — prose, code, paths, emails — becomes `<redacted:str>`;
- **list lengths are preserved**, contents are scrubbed element by element;
- `--help` / `--version` text and log tails have the home dir and any emails
  redacted, with a line cap.

In PROBE mode the raw capture exists **only transiently** inside the temp dir,
which is `rmtree`'d in a `finally` (even on exception or Ctrl-C). The CLI's own
transcript stays in its home dir — the command reads its _structure_, never copies
it. A hidden `--keep-temp` flag retains the raw temp dir for debugging and prints a
loud **"raw retained — do not share"** warning; never paste a `--keep-temp` run.

---

## Walkthrough: finalizing a profile

### 1. Draft a profile

Drop a TOML file in `<project>/.automator/profiles/<name>.toml` with the fields
described in the [README adapter section](../README.md#other-coding-clis). The
contract is the `CLIProfile` / `HookSpec` dataclasses in
[`src/automator/adapters/profile.py`](../src/automator/adapters/profile.py): a
`binary`, a `prompt_template`, bypass flags, a `[hooks]` block picking one of the
config dialects (`claude-settings-json` / `codex-hooks-json` /
`gemini-settings-json` / `copilot-settings-json`) and a native→canonical event
map, and a `usage_parser` (start with `"none"` until you've written one).

### 2. Scan

```bash
bmad-auto probe-adapter <cli> --project .
```

Read three sections of the report:

- **CLI flags** — your profile's launch/bypass flags plus the scrubbed
  `--version` / `--help`, so you can confirm the flags you chose exist.
- **Transcript** — the redacted location, format, size, line count, and modified
  date of the newest transcript the convention glob found.
- **Token usage schema** — the structural key paths (types only, never values) and
  the **token-field candidates** (int leaves whose names look token-ish). When a
  real parser is already declared, its parsed counts are shown as a self-check.

### 3. Probe (confirm the live payload + dialect)

```bash
bmad-auto probe-adapter <cli> --probe --project /tmp/scratch
```

The **Hook payload shape** section now shows, per captured event, the native→
canonical pairing, the payload keys, and the scrubbed payload — so you can confirm
`session_id` / `transcript_path` casing and that the CLI accepted the hook config
for your dialect. If the CLI rejects the config or never fires a hook, the report
says so (with a scrubbed log tail) instead of silently producing nothing.

### 4. Write the `usage_parser`

Turn the report's `token_field_candidates` into a parser in
[`src/automator/tokens.py`](../src/automator/tokens.py), following the existing
ones (`tally` for claude, `tally_codex_rollout`, `tally_gemini_chat`) and
registering it in `read_usage`. The report flags **per-call vs cumulative** as a
human call — a `token_count`-style event that carries running totals (codex) is
read differently from per-message blocks that are summed (claude/gemini). Re-run
scan after wiring the parser: the **parsed counts** self-check should now appear.

---

## Flags reference

| Flag                | Purpose                                                                          |
| ------------------- | -------------------------------------------------------------------------------- |
| `--probe`           | Opt-in live capture (default is scan). Needs a known profile.                    |
| `--transcript PATH` | Inspect this exact transcript file, bypassing convention discovery.              |
| `--session-dir DIR` | Glob this dir (`**/*.jsonl` then `*.json`, newest) — for custom/unknown CLIs.    |
| `--binary NAME`     | Binary to probe for a CLI that has no profile yet (enables a reduced report).    |
| `--model NAME`      | Model passed to the probe turn (PROBE mode).                                     |
| `--timeout SECONDS` | Probe turn timeout (default 90).                                                 |
| `--out FILE`        | Write the report to a file instead of stdout (the only file the command writes). |
| `--json`            | Append a machine-readable JSON block to the report.                              |
| `--keep-temp`       | (hidden, debug) keep the raw probe temp dir — prints a "do not share" warning.   |

Exit codes mirror `validate`: `0` whenever a report is produced (warnings are
fine), `1` only when nothing could be produced. An **unknown CLI with `--binary`**
still yields a _reduced_ report (version/help + discovery, no hook events); an
unknown CLI without `--binary` fails and lists the available profiles.

---

## Worked example: copilot

The bundled `copilot` profile ships with `usage_parser = "none"` — Copilot's
token-usage schema hadn't been captured when the profile landed. That's exactly
the gap `probe-adapter` closes:

```bash
bmad-auto probe-adapter copilot --probe --project /tmp/scratch
```

captures the `Stop` payload (confirming `session_id` / `transcript_path` casing),
locates `~/.copilot/session-state/*/events.jsonl`, and infers its token schema —
the data needed to write a `copilot-*` parser in `tokens.py` and flip the profile's
`usage_parser` off `"none"`. Confirm the `mkdtemp` dir is gone afterward.
