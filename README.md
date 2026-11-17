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
