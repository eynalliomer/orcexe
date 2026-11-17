# orcexe

orcexe is an open-source Python CLI and Claude Code plugin that orchestrates AI coding tools.
It uses only the Python standard library and supports Python 3.9 and later.
orcexe has no telemetry.

Status: **v0.1 in development**.

## orcexe do

```sh
orcexe do "Add a docstring to src/app.py" --repo /path/to/repo
orcexe do "src/app.py'ye docstring ekle" --dry-run --json
```

`do` classifies and routes one fast-lane task, executes one Codex attempt in an
isolated worktree, checks it with the gate, and records the result in the project
ledger. It never asks questions. Only `accepted` prints human landing commands;
review and commit pending worktree edits before using them. Nothing is merged,
and the worktree is always kept. Standard/risky tasks require the future `plan`
command. Codex uses your existing login; `do` does not log in or install tools.

Options: `--repo` defaults to cwd; `--timeout` defaults to 1800 seconds; `--json`
prints one redacted summary. `--model-constraint` accepts a JSON route constraint
object (`tools`, `deny_models`, `exclude_families`, or `force` with tool/model/effort).
`--dry-run` classifies and probes access/routes without a model call or any project
write: no lock, ledger event, worktree, or brief.

Reports and executor evidence live in `.orcexe/runs/<run-id>/`; the runs directory
is private (0700) and `.orcexe/` is git-excluded. Reports redact task previews;
`brief-src.md` and the adapter's `brief.md` deliberately contain the verbatim task
so the executor can read it. Treat these private briefs as sensitive.

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
