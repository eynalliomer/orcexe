# orcexe

orcexe is an open-source Python CLI and Claude Code plugin that orchestrates AI coding tools.
It uses only the Python standard library and supports Python 3.9 and later.
orcexe has no telemetry.

Status: **v0.1.0 walking skeleton** (local tag, not published). See `CHANGELOG.md` for known gaps.

## orcexe do

```sh
orcexe do "Add a docstring to src/app.py" --repo /path/to/repo
orcexe do "src/app.py'ye docstring ekle" --dry-run --json
```

`do` classifies and routes one fast-lane task, executes one attempt using Codex, Claude Code, or Cursor in an
isolated worktree, checks it with the gate, and records the result in the project
ledger. It never asks questions. Only `accepted` prints human landing commands;
review and commit pending worktree edits before using them. Nothing is merged,
and the worktree is always kept. Standard/risky tasks require the future `plan`
command. Each tool uses your existing login; `do` does not log in or install tools.

Options: `--repo` defaults to cwd; `--timeout` defaults to 1800 seconds; `--json`
prints one redacted summary. `--model-constraint` accepts a JSON route constraint
object (`tools`, `deny_models`, `exclude_families`, or `force` with tool/model/effort).
`--dry-run` classifies and probes access/routes without a model call or any project
write: no lock, ledger event, worktree, or brief.

Reports and executor evidence live in `.orcexe/runs/<run-id>/`; the runs directory
is private (0700) and `.orcexe/` is git-excluded. Reports redact task previews;
`brief-src.md` and the adapter's `brief.md` (under `brief/` for Claude/Cursor) deliberately contain the verbatim task
so the executor can read it. Brief copies use mode 0600. Treat these private briefs as sensitive.

The gate runs the project's own test, lint, and build commands without a network
sandbox, from definitions frozen before execution. Tool project hooks may also
run. Cursor starts with `--trust` and uses its sandbox for the write profile.
Claude/Cursor receive `--add-dir` access only to the run's `brief/` directory.

orcexe writes project state and reports under `.orcexe/`, managed worktrees under
`<repo>-wt/`, and cached reports, quota observations, and temporary gate checkouts
under `${ORCEXE_STATE_DIR:-~/.cache/orcexe}`. The Claude quota cache is
`quota-claude.json` there; observations expire after six hours and reset windows
stop blocking routing. Delete that file to clear the cache immediately.
Codex may update its trust config (below); orcexe records additions in
`.orcexe/trust-ledger.json`. Cursor's model keys in its user config are restored
after a run.

## Codex and Cursor trust entries

Codex can add worktree trust entries to `${CODEX_HOME:-~/.codex}/config.toml` (E1).
`do` and write-profile `run` record newly observed project headers in the private, git-excluded
`.orcexe/trust-ledger.json`; pre-existing user entries are never recorded.

Cursor's `--model` persists default model settings, which orcexe restores after
each run (C1). Its `--trust` also creates directories under
`${CURSOR_CONFIG_DIR:-~/.cursor}/projects/` for both the workspace and each
`--add-dir` path (C2, observed 2026-11-18). The slug rule is inferred from 2 real
samples (2026-11-18): replace each run outside `[A-Za-z0-9]` in the absolute path
with `-`, then strip leading/trailing `-`; spaces/unicode are unverified. Every
Cursor write/review run records only newly observed directory names and their
workspace paths when uniquely matched; unmatched or colliding slugs stay unknown.
Contents may include conversation transcripts, authentication files and worker
logs; orcexe never reads these contents.

```sh
orcexe trust list --repo /path/to/repo
orcexe trust clean --repo /path/to/repo           # preview (default)
orcexe trust clean --repo /path/to/repo --apply --yes
```

Cleanup removes only recorded tables whose directories are gone. Live paths and
tables with user edits are kept. Apply creates a private backup next to the
config and refuses concurrent config changes. Without `--yes`, apply requires
confirmation on a TTY. Both commands support `--json`, `--codex-home`, and
`--repo` (cwd by default, resolved to the main worktree). Exit codes: 0 success,
1 confirmation declined or unavailable, 2 unreadable/unwritable state or a
config change during cleanup. Header observations retain no other config lines.
Cursor cleanup selects only recorded dirs whose known workspace paths no longer
exist, refuses symlinks, and moves them into
`projects/.orcexe-removed/<UTC>/` without deletion or reading contents. Live,
unknown and pre-existing user dirs are kept; removed entries remain in the ledger.

## Local readiness

Run `orcexe doctor` first. It checks OS/architecture, Python, Git, repository disk
space, optional `gh`, tool manifests/versions, subscription login, model ids,
quota, billing override names, trust-ledger counts, and host capabilities H1–H7.
It never installs, logs in, changes tool config, or calls a model. Every warning
or failure includes a fix to run yourself; `--fix` is not supported in v0.1.

`--json` prints the strict `orcexe.env.v1` report, also written atomically with
mode 0600 to `${ORCEXE_STATE_DIR:-~/.cache/orcexe}/env.json`. Reports omit account
details and replace binary paths with `<private>/codex`, `claude`, or `agent`.
The 60-second cache avoids repeated probes (`--no-cache` refreshes). `--quick`
skips quota and all Codex app-server calls, using cached Codex model ids or null;
`--tool codex|claude-code|cursor` limits tool checks. Doctor exits 0 with no
failures, 1 with failures, or 2 if diagnostics cannot run.

Use `orcexe probe <tool> <detect|auth|models|quota> [--json]` for one model-free
adapter call. It exits 0 for an answer, 1 for a reported tool error, or 2 for an
absent tool/unrecognised answer. Claude's unavailable model-free list exits 1.
Host detection uses environment names only: `CODEX_THREAD_ID` or `CODEX_SANDBOX`
selects Codex ahead of an inherited `CLAUDECODE`; otherwise `CLAUDECODE` selects
Claude Code, or the host is `none`. Codex capabilities are declared `info`.
Claude Code H1/H2 report measured subprocess/skill-file evidence, while other
capabilities remain declarations.

## Run one executor by hand

Write a UTF-8 brief (at most 1 MiB), then choose a Git worktree and executor:

```sh
orcexe run --tool codex --model gpt-6-luna --effort low \
  --brief brief.md --workdir ../project-worktree --profile write
```

Tools: `codex`, `claude-code`, `cursor`. The default profile is `review`;
`write` requires a linked worktree (`git worktree add …`). Every run requires
Git. Optional flags: `--run-id ID`, `--timeout-min 30`, `--stall-min 5`, `--json`.
Private artifacts live under the main worktree's Git-excluded
`.orcexe/runs/<run-id>/`; run ids cannot be reused. Codex write trust additions
are recorded in the repository trust ledger; Cursor model settings are restored.

The command reports observed `orcexe.run.v1` evidence, with no classification,
gate or landing. Exit 0 means `completed` (with changes for write); exit 1 means
`failed`; exit 2 means quota, auth, timeout, stalled, blocked, cancelled, an empty
write diff, a `pipes_held` warning, or invalid usage. Pre-existing dirty paths are excluded from the
run's changes. Agent self-reports are informational. SIGINT/SIGTERM/SIGHUP stop
the executor and publish a validated, atomic `cancelled` receipt.

## Using orcexe from Claude Code

Add this repository as a plugin marketplace (a local checkout path or the GitHub
repository), install the plugin, then check readiness:

```text
/plugin marketplace add /path/to/orcexe        # or: /plugin marketplace add eynalliomer/orcexe
/plugin install orcexe@orcexe
/orcexe doctor
```

The plugin ships the `orcexe` skill and the `/orcexe` command
(`/orcexe doctor | do "<task>" | run … | trust list|clean`). Both call
`python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe"`. The skill tells Claude to show doctor
fixes verbatim, report `do` verdicts from the gate rather than the agent's own
claim, and never run landing commands or `trust clean --apply` without your
explicit request. `python3 scripts/check_marketplace.py` validates the
marketplace, plugin manifest, versions, skill and command files.

## Internal project discovery

The internal `orcexe intake [--repo PATH] [--json]` command discovers an existing
project without models, network access, or executing project commands. It reads
only allow-listed committed HEAD blobs (256 KiB per file, 4 MiB total); dirty and
untracked files only affect status counts. The root `.orcexe/` state directory
is excluded from status counts, size and discovery. It records stack, candidate
commands, CI, opaque instruction fingerprints, hook/MCP declarations, docs and size in the
main worktree's `.orcexe/intake/u1.json` (0600, directory 0700). `--json` prints
the same document. Candidates have `verified: false`; secrets are never fetched,
MCP arguments/environment and instruction prose are omitted, and unsupported or
malformed syntax is recorded in `meta.unseen`. Structured tool settings expose
only hook declaration names. Python 3.9/3.10 use a conservative TOML subset;
unsupported constructs are also unseen. An unborn repository produces empty
evidence with `no_head`; a non-repository exits 2. This internal command is not
part of the plugin command menu.

## Running the tests

From the repository root, run `python3 tests/tools/run_tests.py`. The stdlib-only
runner discovers the same `test*.py` modules as `python3 -m unittest discover`,
runs each in a subprocess with a private home and temp directory, and defaults
to one worker per CPU. Use `-j 1` for serial module execution; ordinary unittest
discovery also remains supported. Each module has a 300-second timeout multiplied
by `ORCEXE_TEST_TIME_SCALE` (`--timeout` overrides the base). Previous module
durations in the ignored `tests/.durations.json` schedule longer modules first.

The default suite keeps all named regressions and deterministic complexity
guards, prints `sampled=` counts for generated subsets, and explicitly skips
timing-ratio/megabyte-latency checks and the manifest mutation timing limit
(all manifest mutants and 100,000 ledger records are still checked). The full run is
`ORCEXE_SLOW_TESTS=1 python3 tests/tools/run_tests.py`; run it in slow-suite CI
and whenever `redact*.py`, `classify.py`, or `miniyaml.py` changes. It restores
the complete generated sets and timing checks. Use `--timeout 1800` if the full
redaction module exceeds 300 seconds on a slower machine.

For five runs under twice the CPU count in busy-loop workers, use
`python3 tests/tools/stress_suite.py --runs 5 --scale 3`. Its default runner is
`parallel`; `--runner discover` exercises ordinary serial unittest discovery.

## CI and cost

CI runs the marketplace check and parallel test runner on Ubuntu, macOS and
Windows with Python 3.9 and 3.13 (six jobs). Native Windows support is unverified
in v0.1: its two jobs are experimental and continue on error, with failures still
reported. Markdown-only changes skip CI, except plugin skills and commands.
The full slow suite runs on Ubuntu/Python 3.9 weekly, manually, and on pull
requests touching `redact*.py`, `classify.py` or `miniyaml.py`.

Public repositories run standard GitHub-hosted runners for free. For a private
copy, estimate about 76 billable minutes per full matrix run: Ubuntu 2 jobs ×
~2 minutes + macOS 2 × ~3 minutes × 10 multiplier + Windows 2 × ~3 minutes × 2.
This excludes the separate slow suite; actual usage depends on runtime and the
account's included minutes. Drop macOS/Windows from the matrix in
`.github/workflows/ci.yml` to reduce cost.
