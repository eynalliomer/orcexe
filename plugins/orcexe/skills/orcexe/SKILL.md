---
name: orcexe
description: Use when the user wants another AI coding tool (Codex, Claude Code, Cursor) to do a small, well-scoped
  change in this git repository and have it checked before landing, or asks to check whether orcexe/the AI CLIs are
  ready here ("orcexe doctor", "is codex set up", "run this with codex").
---

# orcexe

## What orcexe does

orcexe classifies a task and picks a tool/model from shipped priors; it never tests models in the user's project.
It runs the chosen tool in an isolated git worktree, gates the result with the project's own checks, and records a ledger.
Landing stays with the human: orcexe never merges, and neither do you unless asked.

## How to call it

Always invoke the bundled launcher through Python; do not rely on the executable bit:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" <command> [options]
```

Run it from the user's repository (or pass `--repo` where the command accepts it). Add `--json` when you need to parse
the result; otherwise relay the human output.

## `doctor` first

Run `doctor` when the user asks about setup ("is codex set up?") and before the first `do` in a session:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" doctor          # full check
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" doctor --quick  # skips quota and Codex app-server probes
```

- Other flags: `--tool codex|claude-code|cursor`, `--no-cache` (results are cached for 60 s), `--json`.
- Summarise the PASS/WARN/FAIL lines briefly; INFO lines only when relevant.
- Show every `→ fix` line verbatim. The user runs those fixes.
- Never log in, install, update or configure a tool yourself, and never work around auth.
- Exit codes: 0 no failures, 1 at least one FAIL, 2 doctor could not run.

## `do "<task>"`

Use for one small, single-file task: a comment, docstring, typo or documentation edit.

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" do "Add a docstring to src/app.py"
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" do "Fix the typo in README.md" --dry-run
```

Options: `--repo PATH` (default: cwd), `--dry-run`, `--json`, `--timeout SECONDS` (default 1800),
`--model-constraint JSON` (keys `tools`, `deny_models`, `exclude_families`, or `force`; only if the user asks).

Pass the user's task text as given. Then report the `Verdict:` line and act on it:

- `planned` (`--dry-run`): show the route and allowed file. Nothing ran and nothing was written.
- `accepted`: show the changed file list and the printed human landing command.
  **Do not run the land command** unless the user explicitly asks you to in this conversation.
- `blocked` with lane standard/risky: tell the user this task needs the standard/risky lane (v0.2+).
  Do the work yourself only if the user asks. Never retry with reworded text to sneak into the fast lane.
- `blocked` for another reason (route/auth/quota, missing git identity, another `do` running, invalid
  `--model-constraint`, not a git repository): show the message; suggest `doctor` for tool problems.
- `escalated`, `needs_fix`, `failed`, `unverified`, `cancelled`: show the `Failed check:` line and its detail, plus
  the run directory to inspect. Never call the task done.

The `agent says:` line is the executor's own claim. Only the gate verdict counts.

## `run` (manual, advanced)

Use only when the user supplies a brief file and a worktree and names the tool, model and effort:

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" run --tool codex|claude-code|cursor --model MODEL --effort EFFORT \
  --brief brief.md --workdir ../repo-worktree [--profile review|write] [--run-id ID] \
  [--timeout-min 30] [--stall-min 5] [--json]
```

- Explain that `run` has **no gate**: it reports observed status and changed files, nothing is checked or landed.
- `--profile write` refuses the main worktree; the user creates a linked one with `git worktree add …`.
- Report the status, model requested/actual, changed files, warnings and evidence paths.

## `trust list` / `trust clean`

`codex exec` can add `[projects."<path>"]` trust entries to `${CODEX_HOME:-~/.codex}/config.toml` for orcexe
worktrees. orcexe records only entries that appeared during its own runs in `.orcexe/trust-ledger.json`.

```sh
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" trust list
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" trust clean                  # dry run (default)
python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" trust clean --apply --yes    # only with the user's explicit OK
```

- `clean` defaults to a dry run that lists candidates (recorded entries whose directories are gone).
- Use `--apply` only after the user explicitly approves the listed candidates. A private backup is written.
- Options: `--repo`, `--codex-home`, `--json`. Exit codes: 0 ok, 1 not confirmed, 2 unreadable or changed state.

## Never

- Pass API keys or set billing variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `CODEX_API_KEY`, `CURSOR_API_KEY`, …).
- Use `--force`, `--yolo`, `--dangerously-*` or similar bypass flags with orcexe or the underlying tools.
- Edit `~/.codex`, `~/.claude` or `~/.cursor`, or anything under `.orcexe/`, by hand.
- Run the landing command, merge, cherry-pick or commit the result without the user's explicit request.
- Treat the agent's own "done" (`agent says:`, `ORCEXE_EXIT: DONE`) as success.

## Exit codes

`do`:

| Code | Meaning |
|------|---------|
| 0 | `accepted`, or a `--dry-run` route was `planned` |
| 1 | failed / gate rejected (`needs_fix`, `escalated`, `failed`, `unverified`), cancelled, another `do` running, missing git identity, no usable route |
| 2 | invalid usage, including an invalid `--model-constraint` |
| 4 | auth: every candidate tool lacked a login, or the executor hit an auth error |
| 5 | quota: every candidate tool was out of quota, or the executor hit a quota limit |
| 6 | executor timed out or stalled |
| 7 | unsupported: standard/risky lane, or not a git repository |
| 70 | internal orcexe error |

`run`:

| Code | Meaning |
|------|---------|
| 0 | `completed` (with changes for `--profile write`) |
| 1 | `failed` |
| 2 | not run or not completed: blocked, quota, auth, timeout, stalled, cancelled, empty write diff, invalid usage |
