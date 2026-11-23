# orcexe

orcexe is an open-source Python CLI and Claude Code plugin that orchestrates AI coding tools.
It uses only the Python standard library and supports Python 3.9 and later.
orcexe has no telemetry.

Status: **v0.2.0** (local, not published): existing-project map, plan and standard lane. See `CHANGELOG.md` for known gaps.

## v0.2 flow

Start with `orcexe map`, then use `orcexe questions` and `orcexe answer` to
resolve the saved form. Run `orcexe plan --goal "…"`, review its tasks and
`plan.md`, and approve with `orcexe plan --approve PLAN_ID`. Execute the approved
plan with `orcexe plan --run PLAN_ID`, or one task with
`orcexe task --plan PLAN_ID --task T1`. Read `orcexe status` and the saved
evidence, review the frozen commit or integration chain, then run the printed
Git landing command yourself. Landing is always manual. `do` remains the short
fast-lane entry point; standard work uses the approved-plan flow.

The standard lane freezes `contract.lite.json` before dispatch: allowed files,
done criteria, integration base and a shared budget of three attempts. Candidate
project checks precede L0 test-count/skip/assertion checks and a red-first rerun
of changed tests on the base. An independent L1 evaluator judges each done
criterion. Failed gates or rubric criteria feed the next attempt; missing or
invalid L1 evidence leaves `gate_passed`, not full acceptance. Scope/risk or
control-state violations stop immediately. The detailed task section below
explains the exact checks and terminal states.

## Gate isolation

Candidate gate checks, L0 baseline counts and red-first checks each get a fresh
temp HOME and scratch directory. macOS uses Seatbelt (`sandbox-exec`); Linux uses
bubblewrap (`bwrap`) with a read-only root, writable checkout/scratch and private
network. Sandboxed project steps have network off and cannot write outside the
checkout and their temporary scratch/HOME. Descendant processes inherit the
policy; on macOS the profile also refuses LaunchServices opens and Apple events,
so a step cannot start an app outside it (see below for the exact limits).
On other systems, or when sandbox creation is denied, steps still run with a
temp HOME but without OS confinement: `isolation_unavailable` prevents acceptance
(`task`: `gate_passed`, exit 0; `do`: `unverified`, exit 1). The reports record
`seatbelt`, `bwrap` or `none`; do not interpret an exit of zero alone as acceptance.

Gate checks run in a plain file checkout of the candidate tree: it has no
`.git`, so project checks must not depend on the repository's own git metadata
or history. orcexe's default suite follows this rule (checked by
`tests/unit/test_default_suite_no_git.py`); budgets that scan this repository's
history live in `tests/slow/`.

Manager and evaluator copies exclude repository hooks, instructions and tool
customizations. Claude manager/evaluator children use `--restricted --safe-mode`;
a Claude **executor** child loads the user's own Claude hooks/plugins. Codex and
Cursor read their own user config. Their own sandboxes and configuration are
separate from the OS isolation around project checks.

## Write locations and manual cleanup

orcexe itself writes nothing in `~` except its documented state directory.
Executor tools can have their own config/trust side effects listed below.
The OS/runtime may also create its own caches: macOS system Python can create
`~/Library/Caches/com.apple.python/`; the security pack treats only that runtime
cache as an OS-owned exception, separately from orcexe state.

| Location | Contents and ownership |
| --- | --- |
| Main repository `.orcexe/` | Intake `intake/u1.json`, `u2.json`, `u3.json`; slop reports, project map, history/direction, `form.json`, `decisions.jsonl`, `plan.json`/`plan.md`, `ledger.jsonl`, trust ledger, `current-state.md` and `status.md`. Publication metadata/generations, `.plan-state/`, human-edit backups/proposals, dispatch locks, tamper marker, recovery `archive/`, and `runs/` evidence also live here. |
| `.orcexe/runs/<id>/` | Private briefs, `contract.lite.json`, attempt logs/results, gate/L0/L1 evidence and per-run status. Completed work and evidence are retained. |
| `${ORCEXE_STATE_DIR:-~/.cache/orcexe}/` | `env.json` readiness cache, manager run evidence under `runs/`, Claude runtime files and disposable `gate/` checkouts. Set `ORCEXE_STATE_DIR` to an absolute path outside the repository to relocate state; manager runs (`map`, `plan`) reject a state dir inside the repository or its main worktree with `not_seen: state_dir_in_repository` (exit 2). |
| Sibling `<repo>-wt/<plan>/<task>-<tool>/` | Managed executor worktrees, including executor edits. Interrupted task worktrees are archived/removed; completed ones remain for inspection. |
| Git common directory | `info/exclude` gets `/.orcexe/`; `info/orcexe-worktrees/` stores creation receipts. Linked-worktree metadata, objects and `orcexe/…` branches are created by Git. Frozen acceptance refs use `refs/orcexe/accepted/…`; plan integration chains use `refs/orcexe/plans/…`. |
| OS temporary directory | Manager/evaluator copies, Git scratch, and per-project-step scratch/temp HOME; context managers remove these on normal exit. A crash can leave temporary directories. |
| Tool-owned user config | Codex may append project trust to `${CODEX_HOME:-~/.codex}/config.toml`. Cursor may create `${CURSOR_CONFIG_DIR:-~/.cursor}/projects/` trust/runtime files; its five saved model keys are restored after execution. Claude executor hooks/plugins can have user-defined effects. See the trust section. |

Until the v0.5 `clean` command exists, clean up by hand after stopping active
runs and saving any changes/evidence you need. Use `git worktree list` and inspect
each managed path; remove it with `git worktree remove <exact-path>` after saving
its uncommitted changes. Delete only its reviewed `orcexe/…` branch with
`git branch -D <exact-branch>`. List private refs with
`git for-each-ref refs/orcexe/`, then delete individual reviewed refs with
`git update-ref -d <exact-ref>`. Keep refs until their commits have been landed
or saved elsewhere.

After that, remove the chosen repository's `.orcexe/` files or directory and its
state-cache/run files under the resolved `ORCEXE_STATE_DIR` (or
`~/.cache/orcexe`). This discards approvals, history, resume state and archives.
The state directory can be shared by repositories: inspect its contents before
removing it. Remove empty sibling worktree folders, and review the matching Git
creation receipts and `info/exclude` line if you want all bookkeeping gone.
Never delete Git's registered worktree metadata by hand while a worktree exists.
Use `trust list` and review `trust clean` before applying it; Cursor project dirs
need separate manual review/removal. Do not delete an entire tool config to
remove an orcexe trust entry. A `state-tampered` marker requires investigation and
trusted-state restoration before removal; it is not a routine cache file.

## orcexe do

```sh
orcexe do "Add a docstring to src/app.py" --repo /path/to/repo
orcexe do "src/app.py'ye docstring ekle" --dry-run --json
```

`do` classifies and routes one fast-lane task, executes one attempt using Codex, Claude Code, or Cursor in an
isolated worktree, checks it with the gate, and records the result in the project
ledger. It never asks questions. Only `accepted` prints human landing commands;
review and commit pending worktree edits before using them. Nothing is merged,
and the worktree is always kept. Standard and risky tasks use an approved `plan` and the `task` command (risky ones behind a per-task contract approval). Each tool uses your existing login; `do` does not log in or install tools.

Options: `--repo` defaults to cwd; `--timeout` defaults to 1800 seconds; `--json`
prints one redacted summary. `--model-constraint` accepts a JSON route constraint
object (`tools`, `deny_models`, `exclude_families`, or `force` with tool/model/effort).
`--dry-run` classifies and probes access/routes without a model call or any project
write: no lock, ledger event, worktree, or brief.

Before any lock or ledger write, `do` requires `.orcexe` to be absent or a real
directory at the main worktree root. A symlink, dangling link or regular file
there is a usage error (exit 2) naming `.orcexe` and asking to remove or rename
it; nothing is created. A `--repo` path that does not exist exits 2; an
existing directory that is not a Git repository keeps the everyday-mode exit 7. Task text must be encodable UTF-8; lone surrogates are
rejected with `surrogates not allowed` before any ledger event.

Reports and executor evidence live in `.orcexe/runs/<run-id>/`; the runs directory
is private (0700) and `.orcexe/` is git-excluded. Reports redact task previews;
`brief-src.md` and the adapter's `brief.md` (under `brief/` for Claude/Cursor) deliberately contain the verbatim task
so the executor can read it. Brief copies use mode 0600. Treat these private briefs as sensitive.

The gate runs the project's own test, lint, and build commands from definitions
frozen before execution, with OS write and outbound-network isolation where available. Tool project hooks may also
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

## Fast-lane vocabulary

Fast vocabulary: `T` is one uniquely resolved tracked target, bare or backtick-quoted; `[x]` is optional and `(x|y)` lists exact alternatives. Grammar words ignore case and fold Turkish I; targets keep exact spelling; Turkish nouns and apostrophe target suffixes use the existing closed suffix set (at most three), e.g. `README.md’ye örnek ekle` or `README.md’deki yazım hatasını düzelt`. Only README has bare stem shorthand; other stems need backticks. The generic document-fix form requires an explicit extension. The whole task must fit 200 characters and 25 tokens. Extra text, multiple mentions, unknown tokens, shell syntax, URLs, outside paths and Unicode disguises block fast. Doc forms accept `.md .markdown .rst .txt .adoc`; source forms accept ordinary supported source files. Risk/destructive/outward words, risk-map additions and denied targets always take precedence: security/licence files, CI, manifests, locks, env files, migrations, Dockerfiles, i18n data, generated/vendor/build files and command directories stay off fast.

| Intent / target class | English full task | Turkish full task |
| --- | --- | --- |
| Add/fix docstring / source | `add [a ]docstring to T`; `fix [the ]docstring (in\|of) T` | `T docstring ekle`; `T docstring düzelt` |
| Add comment / source | `add [a ]comment to T` | `T yorum ekle` |
| Add section or example / doc | `add [a ](usage\|installation) (section\|example) to T`; `add an (usage\|installation) example to T` | `T (kurulum\|kullanım) (bölüm\|örnek) ekle` |
| Add example / doc | `add an example to T` | `T örnek ekle` |
| Fix typo / doc or source | `fix [the ]typo in T` | `T [yorum ]yazım hata düzelt` |
| Clarify/update / doc | `clarify T`; `update T` | `T [paragraf ]netleştir`; `T güncelle` |
| Fix document / doc | `fix T` | `T düzelt`; `düzelt T` |

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

Review permissions differ by tool:

- Claude uses `--permission-mode plan --disallowedTools Bash,WebFetch,WebSearch`.
  Read/Grep/Glob remain available; Bash and web fetching/search are denied, and
  plan mode restricts edits. Plan mode alone permits Bash. User/project hooks
  may still run in manual `run`; this is a tool policy, not an OS sandbox.
- Codex uses `-s read-only`: it can read files and run shell commands within
  the sandbox, but cannot write project files. This does not prevent executing
  project code; network access follows Codex's sandbox configuration.
- Cursor uses `--mode plan`: it can inspect files and plan changes, with edits
  restricted by Cursor's plan policy. It can execute commands, including project
  code. No shell/web deny list or OS read-only
  sandbox is added for review; shell/web blocking is not guaranteed. Hooks,
  trust directories and restored model settings have the side effects above.

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
Onboarding does not execute project code; commands are verified by the first task's gate.

`orcexe map [--repo PATH] [--json] [--tool claude-code] [--model M] [--full]`
refreshes U1, slop, and U2/U3, then makes at most one dedicated manager-profile call with a
900-second timeout and 300-second stall budget in a disposable raw copy of HEAD. Project tool configurations,
secrets, excluded paths and dirty/untracked content are omitted from that copy.
`map` uses Claude only, with `--permission-mode plan --restricted --safe-mode
--strict-mcp-config --tools Read,Glob,Grep`. These tool-level flags are the actual
isolation boundary: user/project/local settings, CLAUDE.md, skills, plugins,
hooks, MCP servers and custom commands are not loaded. `--bare` remains forbidden
because it breaks subscription authentication. Codex and
Cursor review profiles can execute commands and therefore cannot manage onboarding;
explicit `--tool codex|cursor` exits 2 with `not_seen: manager_tool_not_read_only`.
The default manager model is Claude Opus 5.5; `--model` overrides it.
The brief treats repository text as
untrusted data and instructs the manager to use only file-reading tools. Its
compact catalog carries evidence ids, total counts and at most three examples
per section, with duplicate observations omitted; long findings lists are summarized.
As defence in depth, agent instruction files (`CLAUDE.md`, `CLAUDE.local.md`,
`AGENTS.md`, `GEMINI.md`, `.cursorrules`, Copilot instructions), agent directories
(`.agents`, `.claude`, `.cursor`, `.codex`), security configurations and hook files
are removed at every depth using NFC normalization and casefold matching, including
case variants such as `.Claude/settings.json` and `Agents.MD`.
Manager briefs and receipts live under `${ORCEXE_STATE_DIR:-~/.cache/orcexe}/runs/`;
the manager receives only the disposable copy and its external run directory.

Mapping is incremental by default. A valid previous map whose `intake_sha` is an
ancestor of HEAD supplies the update base. The manager gets the previous map as
fenced untrusted data and a catalog of added, modified, renamed and deleted paths;
deleted entries contain paths only. Its copy contains only changed safe files
still present at HEAD. It may revise the interpreted sections; previous entries
omitted from its response are retained and evidence is checked again at HEAD.
`update` records `mode` (`incremental` or `full`), `base` (SHA or null) and `changed`
(changed path count; renames count both old and new paths). A missing/invalid map,
rewritten history, `--full`, or changes to more than 40% of HEAD's tracked files
cause a full rebuild. At unchanged HEAD with identical model-free inputs, `map`
reports no change and skips routing and the manager. `--full` forces a rebuild.

Private publication receipts in `.orcexe/.publish-receipt.json` (and
`.orcexe/.plan-state/` for plans) store SHA-256
digests and generated baselines, outside agent worktrees. Human additions and
changes to JSON `risk_map`, `do_not_touch`, `conventions` and `unknowns` survive
verbatim with `source: human`, including empty evidence lists. Entry identity is
`path_glob`, convention `rule`, or unknown `question`. Human restrictions affect
questions, plan lanes and task risk checks like other entries. Deleted entries
stay deleted; a manager re-proposal with new resolved evidence goes in `proposed`
and is never applied automatically. Unknowns accept an optional evidence list for
these re-proposals. An unreadable/invalid old map is retained
as `project-map.human-<sha256>.json` before a full rebuild.

Edited generated Markdown is kept intact. New text goes beside it as
`<name>.proposed.md`; an edited proposal also survives (additional proposals get
numbered names). `human_edit_kept` reports the retained paths on stderr and a
successful map still exits 0. This includes map, history, direction, slop, plan
and status text.

`map`, `plan` proposal/approval, `task` and `plan --run` maintain private (0600)
`.orcexe/current-state.md`: short HEAD, map update mode/date, unknown count,
current plan ID/status, each task's ledger state, chain tip and manual land
command. Its facts come from the CLI map, plan, ledger and refs. This file is
always regenerated, with every block between `<!-- orcexe:human -->` and
`<!-- /orcexe:human -->` preserved verbatim in original order after the generated
summary. An empty placeholder is added only if no human block exists. Unclosed
or nested markers preserve everything from the first opening marker to EOF as
one human block. Edits outside blocks are replaced; before overwriting, the old
file is saved once as private (0600) `current-state.previous.md`, reported as
`current_state_previous_saved`. Current-state never uses `human_edit_kept` or
proposals. Foreign symlink targets are refused.

Manager output (`map`, incremental `map` and `plan`) is tolerant in form: a bare
JSON object, or exactly one ```` ```json ```` fence with prose before or after it that is
ignored, never parsed. Its results still pass human approval. The L1 evaluator
output is strict (see the task section).

Strict manager JSON is checked against HEAD evidence before writing private,
atomic (0600) `.orcexe/project-map.json`, `project-map.md`, `history.md`, and
`direction.md`. Unresolved claims are dropped and counted; uncovered U2 hotspots
are added to the risk map. Globs matching more than half of tracked files cannot
cover hotspots; brace expansion and total match work are bounded. Protective globs
may name secret/excluded areas, while evidence resolution stays strict. Commands remain `unknown` or `missing`, and history
and direction are each limited to 300 Unicode words and 4 KiB. Oversized claims
are dropped and counted; later short claims may still render. The four outputs
share one generation pointer so publication switches the complete set atomically;
a write failure preserves the previous set and reports `not_seen: write_failed`,
including failed discovery writes to a read-only `.orcexe`. Successful swaps remove
old `.map-generation-*` snapshots; the next run removes stale `.map-transaction-*`
entries without following links. A failed rollback retains the old snapshot.
Repository paths are normalized to the worktree root, and a HEAD change during
the manager run prevents publication (`not_seen: head_moved`). `--json` prints
`orcexe.project_map.v1`. Invalid output or an unavailable manager exits 2 with
`not_seen: manager_output_invalid` or `not_seen: manager_unavailable` on stderr,
the latter with `stalled`, `timeout`, `auth`, `quota`, `no_route`, or `failed`
when an observed run/selection reason is available,
preserving any existing map/history/direction files. Discovery refreshes and run
receipts may still be written on failure. The disposable manager copy is removed.
Non-repositories, gitlink-containing HEAD trees and a `.orcexe` that is a symlink,
dangling link or regular file report `not_seen: not_a_repository`,
`not_seen: gitlink_tree` and `not_seen: orcexe_invalid`, respectively. SIGINT/SIGTERM/SIGHUP/SIGQUIT stop the manager process group, remove its copy and
return the conventional `128 + signal` exit code.

## `plan`: tasks and one approval form

`orcexe plan [--repo PATH] --goal TEXT [--json]` recomputes the current goal's
question form from a project map at HEAD. Unanswered questions save `form.json`
and stop before any tool probe or model call; use `answer` and rerun the goal.
Once answered, one Claude-only read-only manager run uses the same isolated copy,
state-directory receipts, budgets and cancellation cleanup as `map`.

The strict `orcexe.manager.plan.v1` output supplies tasks with IDs, goals,
`files.allow` globs, done criteria, dependencies, lanes, sizes and parallel flags.
Task goals are one sentence of at most 300 characters; each of at most eight done
items is at most 300 characters, and each glob is at most 200. Optional task
`notes` hold up to 2,000 characters of implementation guidance and appear in both
`plan.json` and `plan.md`. Both manager briefs declare their validation limits.
Invalid manager output adds a content-free diagnostic such as
`detail: tasks[0].goal too_long (1385 > 300)` after `not_seen: manager_output_invalid`.
The CLI rejects empty done criteria, duplicate IDs, unknown dependencies, cycles,
size L, more than 12 tasks, zero tasks, invalid globs and unordered parallel tasks
whose allowed paths can overlap, including paths that do not yet exist. Rejection
prints `not_seen: plan_rejected` and `violation: <code> <task id or ->` per violation.
The violation codes are `empty_goal`, `empty_allow`, `empty_done`, `duplicate_id`, `unknown_dependency`, `cycle`,
`size_l`, `too_many_tasks`, `no_tasks`, `parallel_overlap`, `invalid_glob`, and `no_test_path`.

<!-- plan-violation-reasons -->
| Violation | Reason |
| --- | --- |
| `empty_goal`, `empty_allow`, `empty_done` | Cleaned goal, allowed paths or done criteria are empty. |
| `duplicate_id`, `unknown_dependency`, `cycle` | Task IDs or dependency graph are invalid. |
| `size_l`, `too_many_tasks`, `no_tasks` | Tasks must have size S/M and number 1–12. |
| `parallel_overlap`, `invalid_glob` | Unordered parallel scopes overlap, or a glob is invalid/exceeds limits. |
| `no_test_path` | A resolved standard/risky task allows non-documentation paths but no L0 test path. |
<!-- /plan-violation-reasons -->

After lane resolution, a standard/risky task allowing any non-documentation path
must allow a test file recognized by L0 `tests_missing`; documentation-only and
fast tasks are exempt. The check uses covered tracked files and each expanded
glob read as a path, with the same test/documentation classification as L0.
Rejected plans publish nothing.
Globs are anchored repo-relative paths: `*` and `?` stay within a segment, `[]`
selects characters, a whole `**` segment spans directories, and `{a,b}` expands
alternatives, including slashes and wildcards inside alternatives. Maps, questions
and plans share this grammar and the same bounded expansion (256 alternatives,
4,096 expanded characters). Absolute paths, traversal and malformed globs fail.
Protective globs may name Git/state directories, secrets and excluded paths;
glob validity checks syntax and bounds independently of executor path policy.
Path comparisons use NFC and casefold on every filesystem. Trailing `/` globs
cover descendants; literal scopes naming directories (from tracked path ancestors or local directory entries) also include
descendants, including future files. Explicit trailing `/` covers untracked directories. Plan lanes apply the same
main-worktree `risk-map.yaml` words and paths and classifier signals as `do`,
in addition to project-map risks, recording signals in `lane_basis`. Plan, gate and
escalation share one repository risk-area list. Any-depth risk patterns raise
the lane only when an allowed glob read as a path, or a tracked file it covers,
matches the risk area. Named risk areas retain overlap checks for future paths;
an ordinary directory glob alone does not imply any-depth risk.
All four commands use the main worktree top level and `.orcexe` directory.
Task text and allowed globs are cleaned before P3; e-mail-like globs are rejected.
Empty cleaned goals/done criteria and empty allow lists report `empty_goal`,
`empty_done` and `empty_allow` violations.

Each task records `lane_basis`: the higher of the manager lane and CLI
classification wins (`fast < standard < risky`). Allowed globs intersecting an
`auth`, `payment`, `data` or `migration` risk entry, or any do-not-touch entry,
raise the lane to risky. An unusable risk/do-not-touch glob conservatively raises
the lane and records `cli:risk_glob_unusable:<glob>` in `lane_basis`.
The Markdown approval form shows each task's lane basis, including the risk class
and area or do-not-touch area that raised its lane.

Plan lanes use classifier `lane.v2`, recorded as `"classifier": "lane.v2"` in
`plan.json` (bound by `content_sha256`). The goal keeps every `do` rule, so no
goal gets a lower lane than `do` gives it; in addition an identifier hides an
action word only when it names a tracked file (full path or file name with
extension) and the sentence does not run it (`run`, `execute`, `launch`,
`python`, `çalıştır`…, a `(` or a `.sh`-style extension). Dotted names hide only
their tracked module part (`gitutil.push` still says `push`), and `commit(s)` or
`curl(1)` are words, not calls. Each done item and the notes are classified too,
and the task lane is the highest of the fields: action shapes (spaced backticks
and fences as commands, calls such as `os.remove(` or `requests.put(`, command
names such as `curl` or `git push`, imperatives, run-verb sentences, hard
destructive words such as `rmtree` or `rm`, uppercase HTTP methods, and state
sentences such as "is pushed to origin" or "dalı origin'e gönder") make a task
risky; a narrow set of exact risk words (`password`, `secret`, `payment`,
`migration`, `production`, `şifre`…) raises too, while other risk words are
only recorded. Soft destructive words (`remove`, `sil`…) keep standard. Basis
lines name their field: `cli:goal:A2:git.push`, `cli:done[1]:A8:pushed`,
`cli:notes:risk_word:token`. Only the goal can produce fast. An unusable
`risk-map.yaml` (corrupt, wrong type, unknown key, unsupported version, dangling
symlink, unreadable) never lowers a text-derived lane and never gives fast.
The classifier is a word list, not an understanding of intent: actions it has
no word for (`aws s3 sync`, `pip install`, `vercel --prod`, `npm run ship`, a
pull request or Slack message, turning off TLS checks, `subprocess.run(cmd)`
with a variable) are not recognized and stay standard.
One access snapshot routes every task through the
`executor` stage. No eligible executor leaves `route.tool: null` and its reason;
it does not reject the plan. Routes include tool, model, effort, confidence and basis.

Success publishes private (0600) `.orcexe/plan.json` (`orcexe.plan.v1`) and
`plan.md` together, preserving the map. The single form shows the goal,
assumptions, each task's allowed files, size, parallel flag, lane, tool/model, dependencies and done criteria, and the
approval command. A plan starts as `proposed` and records HEAD and decision IDs.
This version runs `fast`, `standard` and (T60) `risky` tasks. A task whose final
lane this version cannot run would get `"lane_note": "lane_not_supported"` in
`plan.json` and a `<lane> (lane_not_supported)` lane cell in `plan.md` (only under
the K18 (b) fallback; a plan proposed before risky became runnable is `plan_stale`
with `detail: lanes_changed` at approval: propose again). The form ends
with "Will run after approval" and "Will be deferred" sections, so the operator
sees before approving what an approval would run and what it would park.

`content_sha256` binds the canonical JSON content (sorted keys, compact separators,
UTF-8), excluding `plan_id`, `content_sha256`, `status`, `approved_at` and `approval`.
`plan_id` is `plan-` plus this digest. Approval recomputes it, rechecks cleaned
tasks, P3 and current lanes, rejects empty or changed plans as `plan_tampered`,
and records the digest in the approval decision.

### Partial approval and `deferred` tasks

```sh
orcexe plan --approve PLAN_ID                  # approve the runnable subset, defer the rest
orcexe plan --approve PLAN_ID --defer T2[,T5]  # also defer tasks you do not want run
orcexe plan --approve PLAN_ID --open T3[,T4]   # later: open deferred tasks with a new decision
```

`orcexe plan [--repo PATH] --approve PLAN_ID [--defer IDS] [--json]` requires that
exact proposed plan at the same HEAD. It decides, once and for all at that moment,
which tasks run: a task is `deferred` with `lane_not_supported` (its final lane
cannot run in this version), `no_route` (the plan has no executor route for it),
`deferred_by_human` (`--defer`), or `dependency_deferred` (a dependency is
deferred; `cause` names it). Every transitive dependant of a deferred task is
deferred too. The rest are approved. If nothing is runnable the approval is
refused with `plan_rejected` (exit 2): stderr prints `not_seen: plan_rejected`
and one `deferred: T<n> <reason> (<evidence>)` line per task, nothing is written
and the plan stays `proposed`, so `plan --run` refuses it (`plan_not_approved`).
An unknown `--defer` id is refused the same way (`refused: T<n> task_unknown`).

The plan keeps its `plan_id` and `content_sha256`. Approval marks it `approved`,
records `approved_at`, and writes `plan.approval = {approved, deferred: [{id,
reason, cause, evidence}], approval_sha256, decision_id, at, opened_from}`.
`approval_sha256` is the SHA-256 of the canonical `{plan_id, content_sha256,
classifier, head, approved (sorted), deferred (sorted by id), opened_from, lanes}`
(`lanes`: the lanes this version runs); the reasons are frozen into it and never
recomputed from live access later. One
`orcexe.decision.v1` record (kind `plan_approval`, with the same lists and
`approval_sha256`) is appended to `decisions.jsonl`, and the ledger gets
`decision.recorded {kind, decision_id, plan_id, record_sha256}` (anchored by the
ledger head ref) plus a `task.state pending → deferred` row per deferred task.
`plan.md` then says `Status: approved (2 of 4; 2 deferred)` and keeps the "Will run
after approval" / "Will be deferred" sections (a dependant reads `- T4:
dependency_deferred (depends on T3)`). It suggests `--open` only for a task that can
be opened (deferred by you, or only behind such tasks) and lists the rest as
"Cannot be opened in this version". Every `plan --goal` publication first writes
`plan.proposed {plan_id}` to the ledger (under the dispatch lock, so a running
plan is not disturbed: `task_running`); decision anchors before the latest
proposal, of any plan, no longer authorize dispatch.

Dispatch permission is the latest decision's `approved` list, never ledger
state: `plan --run` and `--resume` skip deferred tasks (the run ends `partial`,
exit 1) and `orcexe task --plan PLAN_ID --task T3` on a deferred task exits 2
with `task_deferred`. Before any dispatch (`task`, `plan --run`, `--open`) the
CLI checks the content hash, HEAD (`plan_stale`), the classifier (a plan whose
`classifier` is not the current `lane.v2` is `plan_stale` with stderr
`detail: classifier_changed`, also when approving a proposal), a missing
`approval` (plans approved before this version are `plan_stale`, never treated
as fully approved), `approval_sha256`, that `approved ∪ deferred` are exactly the
plan's tasks with no overlap, that the deferred set is closed under
dependencies, the plan-derived reasons recomputed (`lane_not_supported`,
`no_route`, `dependency_deferred`), and the latest decision record carrying the
same hash, lists, head and chain. Any mismatch is `plan_tampered`; a change of the
runnable lanes since the decision is `plan_stale` (`detail: lanes_changed`). A
decision record without its `decision.recorded` ledger anchor, a ledger
`deferred → pending` row without an anchored `plan_open` decision, or a trusted
decision that is not the plan's last anchored one or predates the latest
`plan.proposed` (an older, wider approval put back into the files) is
`state_tampered` (stderr `detail: decision_unanchored`, `open_unanchored` or
`decision_superseded`): the decision was written outside orcexe. That first refusal also writes
`approval.revoked {plan_id, reason}` to the ledger (T57 §4): from then on `plan
--run`, `task` and `--open` for the plan exit 2 with `plan_not_approved` and
stderr `detail: approval_revoked`, until a fresh approval; the plan is republished
with `Status: revoked` and `orcexe status` reports `plan_status: revoked`. The fresh
approval is allowed only when the ledger's last word on the plan is
`approval.revoked` (otherwise `plan_not_proposed`), only on a verified ledger
(tamper marker absent, chain and head ref intact, else
`state_tampered`/`ledger_corrupt`/`ledger_head_mismatch`), and only with the
deferrals restated: `plan --approve PLAN_ID --defer IDS` or `--defer none`. A bare
re-approval is refused with `plan_rejected` and lists the tasks you had deferred in
the last genuinely anchored decision (`refused: T2 deferred_by_human (restate with
--defer T2 or --defer none)`), so a tamper plus the recovery hint can never silently
approve a task you deferred. The approval is recomputed from the plan content (a
forged `approval` in `plan.json` is ignored) and starts a new decision chain, so
opened-task rows before it no longer count. Proposing again with `plan --goal`
works too. During `plan --run` the approval is checked again after every
dispatched task, so a decision forged in a task's executor window stops that run
(and revokes the approval) before the next task.
Approval and opening take the repository dispatch lock (`task_running` while a
run holds it) and verify the ledger and its head ref first.

`--open T3[,T4]` runs the same checks on the approved plan, then recomputes
runnability from the plan (its lanes and the routes recorded in it at proposal;
executor access is not probed again): an opened task must be deferred, runnable in
this version, and each dependency must be approved or opened in the same call.
A dependant you leave deferred keeps `dependency_deferred` with its cause
(evidence `depends on T3; not opened with it`). Otherwise the call
is refused with `plan_rejected` and `refused: T<n> <why> (…)` lines, and nothing
is written. A successful open is a new `plan_open` decision chained to the
previous one (`opened_from`), anchored the same way, plus `task.state deferred →
pending` rows; the earlier decision is never changed. Landing moves HEAD, so
after a land `--open` is `plan_stale`: the land form warns that the deferred
tasks close for good for this plan, and they return through a new `plan --goal`.

Threat model: these checks catch edits by the sandboxed executor, L1 or agents,
manager output, accidental human edits and broken files. A local process that
runs as you outside the sandbox and rewrites `.orcexe/`, the ledger and its head
ref consistently is out of scope in v0.3; it could just as well run `git merge`.

Later consumers can call
`planning.content_sha256(plan)` to verify the same content. Corrupt plan/decision
state reports `state_corrupt` (exit 2). Approval makes no model call. Failed validation changes nothing; write failures
roll back the plan files and decision append. Neither command executes tasks.
`--json` prints the saved plan on success; diagnostics go to stderr.
SIGINT/SIGTERM return `128 + signal` after cleanup.

<!-- plan-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `approved` | 0 | The plan approval was recorded. |
| `gitlink_tree` | 2 | The tree contains unsupported submodules. |
| `head_moved` | 2 | HEAD changed while observations were being collected. |
| `internal_error` | 70 | Internal computation failed; inspect the reported step. |
| `manager_output_invalid` | 2 | Manager output failed the closed schema or bounds. |
| `manager_unavailable: auth` | 2 | Manager failed: auth. |
| `manager_unavailable: failed` | 2 | Manager failed: failed. |
| `manager_unavailable: no_route` | 2 | Manager failed: no_route. |
| `manager_unavailable: quota` | 2 | Manager failed: quota. |
| `manager_unavailable: stalled` | 2 | Manager failed: stalled. |
| `manager_unavailable: timeout` | 2 | Manager failed: timeout. |
| `map_missing` | 2 | Generate a project map first. |
| `map_stale` | 2 | Refresh the map at the current HEAD. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `output_symlink` | 2 | An output path is an unexpected symlink. |
| `orcexe_invalid` | 2 | `.orcexe` at the repository root is a symlink or not a directory; remove or rename it. |
| `plan_mismatch` | 2 | The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | No saved plan exists. |
| `ledger_corrupt` | 2 | `--approve`/`--open`: the ledger has an invalid line; run `orcexe ledger repair`. |
| `ledger_head_mismatch` | 2 | `--approve`/`--open`: the ledger tail differs from its anchored head ref; run `orcexe ledger verify`, then `orcexe ledger repair`. |
| `plan_not_approved` | 2 | `--open` needs an approved plan whose approval was not revoked (`detail: approval_revoked`). |
| `plan_not_proposed` | 2 | Only a proposed plan can be approved. |
| `plan_rejected` | 2 | Deterministic P3 checks rejected the proposed tasks, or `--approve`/`--open` would approve or open nothing; stderr names each task. |
| `plan_stale` | 2 | The plan was made for another HEAD (also after landing), its classifier or runnable lanes changed (`detail: classifier_changed`/`lanes_changed`), or it was approved before partial approval. |
| `plan_tampered` | 2 | The plan hash, approval lists, reasons, decision record or task shape is invalid. |
| `proposed` | 0 | A plan is ready for human approval. |
| `questions_invalid` | 2 | The map, goal or question data is invalid. |
| `questions_pending` | 2 | Answer the saved form before planning. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `state_dir_in_repository` | 2 | The resolved `ORCEXE_STATE_DIR` is inside the repository or its main worktree. |
| `state_tampered` | 2 | `--approve`/`--open`: a tamper marker exists, or a decision has no ledger anchor (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`). |
| `task_running` | 2 | `--approve`/`--open`: another dispatch holds the repository lock. |
| `write_failed` | 2 | Publishing private state failed. |
<!-- /plan-exit-reasons -->

## `plan --run`, `--resume` and `orcexe status`

```sh
orcexe plan --run PLAN_ID [--max-calls N] [--max-minutes M] [--resume] [--json] [--repo PATH]
orcexe status [--repo PATH] [--json]
```

`plan --run` validates approval, the canonical hash, tampering and HEAD once
under the existing repository dispatch lock. HEAD must still equal the approved
plan head (`plan_stale` otherwise); landing part of the plan does not relax this
rule. Tasks run sequentially in topological order, choosing plan order among
ready tasks. It reuses `task` for the contract, attempts, L0 and L1 gates.
Only ledger state `accepted` satisfies a dependency. An `exhausted`, `rejected`
or `gate_passed` task blocks all transitive dependents with `dependency_failed`;
independent tasks continue. Deferred tasks and their
dependants are never dispatched: they stay `deferred` with their frozen reason
and the run ends `partial`. A risky task waiting for its contract approval
(`awaiting_approval`) is parked: its dependants stay `pending` (never
`dependency_failed`), the other tasks continue, the run ends `partial`, and both
the run output and `orcexe status` print the exact `orcexe task --approve-contract`
command for the user.

The CLI keeps one integration chain at `refs/orcexe/plans/<plan-id>`, starting
at the approved plan head. Every task, including a single `orcexe task`, starts
from the current chain tip, which is frozen in `contract.lite.head`. An accepted
result advances the chain with a frozen commit parented by the previous tip.
Gate-passed results do not advance it. HEAD, the user's index and working files
stay untouched. This ref is never a branch; landing stays manual. Review the
chain and use the printed `git -C … merge --ff-only refs/orcexe/plans/…` command
while HEAD equals the plan head. When the plan has deferred tasks the land form
also carries `deferred` (their ids) and prints a warning: landing closes them for
good for this plan (HEAD moves, so `--open` refuses with `plan_stale`); open them
before landing, or plan them again with `plan --goal`.

SIGINT/SIGTERM closes the partial attempt in the ledger, removes its worktree
and exits 130 with state `interrupted`. Without `--resume`, an interrupted plan
refuses with exit 2 and `run_interrupted`. With `--resume`, final tasks are
skipped and the interrupted task starts a fresh attempt within its original
three-attempt budget, using the same frozen contract and SHA-256. Interrupted
attempts consume budget. A finished plan re-run does nothing and returns the
same final outcome. Only one plan/task/do dispatch can run per repository.

**Call budget (T61).** `--max-calls N` (positive integer) and `--max-minutes M`
(positive number) bound one `plan --run` invocation. The orchestrator counts every
model call it starts in this invocation, around the call and never from tool
output: each executor attempt, each L1 call (fallback candidates included) and each
auditor call, failed, unavailable and invalid ones included. A call that would make
the count exceed N, or any call once more than M minutes of wall-clock time have
passed since the run began, is not started: the task in progress is left
`interrupted` with reason `budget` (resumable; never `accepted`, no accepted ref, no
chain advance), later tasks stay `pending`, the run ends `partial` (exit 1) with
`stop_reason: "budget"` and `calls` in the JSON, and the ledger records
`plan.budget` (`max_calls`, `calls`, `max_minutes` as decimal text or null,
`elapsed_ms`, `reason` = `max_calls`|`max_minutes`). A call already running is never
cut. `--resume` with a new budget continues: attempt numbers continue and spent
attempts are not refunded. Without either flag nothing is counted and the output is
unchanged (with a flag, `calls` is always reported).

`--json` returns `orcexe.planrun.v1` with `plan_id`, `state`
(`completed|partial|interrupted`), `tasks` and nullable `land` (`ref`, `command`),
plus `calls` (with a budget flag) and `stop_reason: "budget"` (budget stop only).
Each task item has `task_id`, `state`, nullable `reason`, and `attempts`.
Exit 0 requires every task accepted; a finished run with any non-accepted task
exits 1; preconditions exit 2. Internal failures exit 70.

The CLI atomically rewrites `.orcexe/status.md` (0600) after every task, with
plan ID, each task's ID/lane/state/attempts/reason, and the integration landing
command. It lives in the main repository, outside agent worktrees.
`orcexe status` is read-only, uses no model and needs no dispatch lock. It reads
task states from the ledger, ignoring status.md and agent claims. Its JSON
schema is `orcexe.status.v1`: `plan_id`, `plan_status` (the saved plan's approval
status), `tasks` (the same item shape), and nullable `land`. Pending tasks have
zero attempts. Failed preconditions print `not_seen: <reason>` on stderr.

<!-- planrun-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `completed` | 0 | Every task in the plan was accepted. |
| `dependency_failed` | 1 | A dependency was not accepted; this task was blocked. |
| `internal_error` | 70 | Internal computation failed; inspect the reported step. |
| `interrupted` | 130 | Plan execution was interrupted; inspect before resuming. |
| `lane_not_supported` | 1 | This entry point cannot execute the selected lane. |
| `ledger_corrupt` | 2 | The ledger has an invalid line (often a torn tail); run `orcexe ledger repair`. |
| `ledger_head_mismatch` | 2 | The ledger tail differs from its anchored head ref; run `orcexe ledger verify`, then `orcexe ledger repair`. |
| `no_route` | 2 | The task has no eligible saved executor route. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `orcexe_invalid` | 2 | `.orcexe` at the repository root is a symlink or not a directory; remove or rename it. |
| `partial` | 1 | At least one task was not accepted. |
| `plan_mismatch` | 2 | The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | No saved plan exists. |
| `plan_not_approved` | 2 | Approve this plan before executing it; after `approval.revoked` (`detail: approval_revoked`) approve it again. |
| `plan_stale` | 2 | The plan was made for another HEAD, its classifier or runnable lanes changed (`detail: classifier_changed`/`lanes_changed`), or it was approved before partial approval. |
| `plan_tampered` | 2 | The plan hash, approval lists, reasons, decision record or task shape is invalid. |
| `run_interrupted` | 2 | Use --resume to continue an unfinished plan. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `state_tampered` | 2 | Project code, a run or a leftover process changed CLI control state (see `orcexe ledger repair` recovery), or a plan decision has no ledger anchor (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`; propose again). |
| `task_running` | 2 | Another dispatch holds the repository lock. |
<!-- /planrun-exit-reasons -->

## The ledger: head anchor, `ledger verify` and `ledger repair`

```sh
orcexe ledger verify --repo /path/to/repo [--json]
orcexe ledger repair --repo /path/to/repo [--json] [--accept-unanchored]
```

`.orcexe/ledger.jsonl` is a hash chain. A chain alone cannot see a deleted suffix
or an edited last line, so every append also moves the Git ref
`refs/orcexe/ledger-head` (a blob with the last `seq` and the sha256 of the last
line) by compare-and-swap under the ledger lock. A ref someone else moved in between
is refused, never overwritten, and the refused line is removed again. The first
anchoring writes a `ledger.anchored` record, so a later missing ref is reported as
deleted (`ref_deleted`) instead of being adopted again; a ledger written before
T55 is adopted once, after its whole chain verifies. `plan --run`, `task`, `status`
and `do` verify the chain and the head before reading state: a mismatch exits 2 with
`not_seen: ledger_head_mismatch`, an invalid line (for example a torn tail) or a
linked ledger file with `not_seen: ledger_corrupt`, each followed by a line naming
`orcexe ledger repair`. The ref is not part of the main-repository snapshot,
because the CLI moves it.

During one command the CLI also remembers the last record it wrote or verified.
Any other tail it meets later in the same command (a record written by a process
an executor left behind, even between guarded windows and even with the ref moved)
is terminal `state_tampered`: nothing more is appended on top of it, and the
`.orcexe/state-tampered` marker records the last trusted `seq`/sha256. While the
marker exists, `status` and `ledger verify` exit 2 with `state_tampered` instead of
showing the recorded states. Out of scope: a process that, between two commands,
rewrites ledger and ref consistently, empties the ledger and deletes the ref, or
removes a loose ref file to fall back to an older packed value and truncates the
ledger to match.

`ledger verify` exits 0 when the chain and head agree and no marker exists.
`ledger repair` takes the dispatch lock and keeps the trusted prefix: up to the
marker's trusted record when there is one, else up to the head ref's record, else
(a never-anchored ledger) the longest valid prefix. Everything after it, valid
records included, moves byte-for-byte to `.orcexe/ledger.quarantine-<UTC>.jsonl`
(0600, never overwriting an earlier quarantine) before truncation; each moved
record is printed (seq, time, task, event, target state). Then the head ref is
re-anchored. It never changes a valid ledger whose head matches. It refuses (exit 2,
nothing changed) when the head ref names no record of this ledger (deleted suffix,
edited line), when the ref of an anchored ledger was deleted, or when a marker
without a trusted point meets unanchored records; `--accept-unanchored` then anchors
the current valid prefix after you have reviewed the records it prints. Repair also
exits 2 for `not_a_repository`, `orcexe_invalid`, `task_running` (another command
holds `.orcexe/do.lock`) and for a symbolic, unreadable or linked head ref or ledger
file (check `git show-ref refs/orcexe/ledger-head` and the file by hand).

Recovery after `state_tampered`:

1. Run `orcexe ledger repair`; it quarantines every record after the marker's last
   trusted one and prints them.
2. Inspect the quarantine file, `git status` and `.orcexe/` (`plan.json`,
   `decisions.jsonl`, `runs/`) for other changes, and stop any process left behind
   by the executor.
3. Remove `.orcexe/state-tampered`, then continue with `orcexe plan --run ID --resume`
   (or rerun the task).

An empty or unparseable `.orcexe/do.lock` older than 10 seconds is removed with a
warning. A lock that parses as an object without a valid owner pid (`{}`,
`{"pid": null}`) still blocks, because it may be a live owner's rewritten lock; the
warning names it for manual removal. A lock of a dead local process is reclaimed as
before, and live, remote or fresh locks still block.

## Risky lane (T60): contract approval, cross-family auditor, `diagnosing`

```sh
orcexe plan --approve PLAN_ID          # risky tasks run if an eligible auditor exists now, else no_auditor
orcexe task --plan PLAN_ID --task T3   # first time: writes the proposal, exits 2 contract_unapproved
orcexe task --plan PLAN_ID --task T3 --constraint "Keep the public signature."   # optional, <=5, re-proposes
orcexe task --approve-contract PLAN_ID T3 APPROVAL_SHA256    # the user, after reading the proposal
orcexe task --plan PLAN_ID --task T3   # (or plan --run) freezes contract.v1 and runs it
```

**Only the user types `--approve-contract` and `plan --approve … --open`.** A host
agent (Claude Code, Codex, …) never proposes, suggests running or runs these two
commands on the user's behalf. This is a documentation rule: orcexe cannot verify
who typed a command.

- **Approval.** `plan --approve` defers a risky task as `no_auditor` (evidence
  `auditor: none (executor families: …)`) when no cross-family auditor route exists
  at that moment; `--open` recomputes it then. The first `task`/`plan --run` on an
  approved risky task writes `.orcexe/contracts/<plan>/<task>/contract.proposed.json`
  and ledger `contract.proposed {approval_sha256}`, moves the task to
  `awaiting_approval` and exits 2 `contract_unapproved` (a plan run continues with
  the other tasks and ends `partial`). The approval hash covers plan/task IDs,
  goal, files, done, `notes_sha256`, `risk` (`signals`, `areas`), `forbidden_paths`,
  `executor`, `auditor` (an upper bound, not a guarantee), `human_constraints`,
  `budget` and the plan head; the freezing base is outside it.
  `task --approve-contract` writes `decision.v1 {kind: contract_approval}` to
  `decisions.jsonl`, its anchor `decision.recorded` and `contract.approved` to the
  ledger. A hash that is not the current proposal's is `contract_unapproved`
  (`detail: proposal_changed`, not a tamper alarm). The next dispatch freezes
  `orcexe.contract.v1` (`contract.frozen {approval_sha256, base}`) only under an
  anchored approval of exactly the current proposal made after the last freeze;
  an approval line without its ledger anchor is `state_tampered`
  (`detail: contract_unanchored`) and revokes the plan approval.
- **Order per attempt:** executor → gate → L0 → L1 → auditor. L1 must be
  `cross_family` (`same_family`/`cursor_read_only` give `l1_basis_insufficient`).
  The auditor (`roles.yaml` stage `auditor`) is a second, separate read-only call in
  its own throwaway copy. Its family excludes every family of the executor, computed
  at run time from the tool and the actual model of **every attempt** behind the
  cumulative tree (recorded as `attempt.ended.model_actual`, re-read on `--resume`;
  the L1 chain is recomputed each attempt). Vendor tokens anywhere in a model id name
  its family (`claude`/`sonnet`/`opus`/`haiku`/`fable`/`anthropic` → Anthropic,
  `gpt`/`openai`/`codex`/`o3` → OpenAI, `gemini` → Google, `grok` → xAI, …). A
  multi-vendor tool (Cursor) whose actual model is missing, `auto`, `default` or any
  unrecognised slug (other than its own `composer-*`) has an **unknown** family: no L1
  is ever labelled `cross_family` for it (standard lane included) and it has no auditor
  (risky: `l1_basis_insufficient` / `auditor_unavailable`). The auditor
  model differs from the L1 model whenever another eligible cross-family model
  exists; otherwise the L1 model is used, recorded as `same_model_as_l1: true`
  (ledger `audit.started`, `task --json` `auditor`, land form `same_model_as_l1`) and
  shown on its own line `Auditor same model as L1: same_model_as_l1 (…)` in `task`,
  `plan --run`, `status` and `status.md`.
- **Output** is exactly `{"schema":"orcexe.audit.v1","verdict":"pass|fail","findings":[{"id","severity":"P0|P1|P2|P3","path","reason"}]}`,
  parsed strictly (one bare object or one json fence; prose only before it; a block
  quoted from the diff is never a verdict), ≤16 findings, reasons ≤1200 characters,
  ≤256 KiB; `path` (relative, `/`, ≤160) and `reason` are single-line printable text
  (control characters, newlines or separators make the reply invalid), and every
  human-facing print of untrusted text escapes non-printable characters.
  `pass` is valid only without P0/P1/P2 findings. An invalid reply is asked
  once more without spending an attempt; a second one is `gate_passed`/`audit_invalid`.
  `fail` spends the attempt and its findings reach the next brief as untrusted
  feedback. The auditor writing to its copy is `rejected`/`auditor_wrote`; ledger,
  `runs/`, decisions, contracts or plan writes during its call are `state_tampered`
  or `main_repository_changed`. Ledger: `audit.started {tool, model, basis, call,
  same_model_as_l1}`, `audit.verdict {model, basis, verdict, findings, tree, call}`
  (findings as `id|severity|path`, no reason text). Only `fail` verdicts are
  remembered per tree; a `pass` is asked again on `--resume`.
- **Budget:** 3 attempts and at most 3 auditor calls. The same failure class
  (stage, check id, hash of the normalized message: paths, numbers and durations
  masked, first 20 lines), recorded as `stage:<32 hex>` (the check id is shown as a
  separate label). Environmental outcomes are decided from structured results only
  (never from words in details or findings) and never get a class: an executor
  `run_status` failure whose run status is quota/timeout/stalled/auth (or warning
  `quota_suspected`/`executor_error`), a check whose detail is exactly a `*_unavailable`
  code, and `l1_unavailable`/`audit_invalid`/`auditor_unavailable` (no failed check); such an attempt
  is spent, but the task goes on to its next attempt. An unavailable auditor call is
  retried (next candidate, then again) within the 3 auditor calls before
  `auditor_unavailable`. The same class
  twice ends `diagnosing` (`same_failure`, `task.state {class, attempts}`): no third
  attempt, not landable; the next step is yours.
- **Accepted** only when L1 passed `cross_family`, the auditor passed `cross_family`,
  `l1.verdict.tree == audit.verdict.tree ==` the landed tree, and isolation evidence
  exists. The accepted record (chain advance + `task.state accepted`) is written first
  and only then `refs/orcexe/accepted/*`, so a failed record never leaves a ref; the
  landing command is printed only for `accepted`;
  `gate_passed` (any reason), `diagnosing`, `rejected` and `exhausted` produce neither.
- **`--resume`** re-checks the frozen contract's approval like a fresh dispatch
  (anchored, before the freeze, after the latest proposal/revocation); otherwise the task
  is proposed again. `--constraint` with a resume is refused (`contract_unapproved`,
  `detail: constraint_on_resume`). `--open` recomputes the auditor for every risky task it
  opens, also a human-deferred one. The shipped `auditor` stage lists a second model per
  family (`claude-sonnet-5-5`, `gpt-6-sol`), so an auditor other than the L1 model is used
  whenever one is in access.
- **Threat model:** a same-user process outside the sandbox that rewrites `.orcexe/`,
  the ledger and the refs consistently is outside the v0.3 threat model.

## `task`: execute one approved task

```sh
orcexe task --plan PLAN_ID --task T1 --repo /path/to/repo --json --timeout-min 30
```

`task` verifies the saved plan's ID, approval, HEAD and canonical content hash
against its approval decision before creating a worktree or dispatching a tool.
Dependencies must have ledger state `accepted`. It uses the
approved task's tool/model/effort route. Only a task in the latest decision's
`approved` list is dispatched; a deferred task (unassigned routes and risky tasks
without an eligible auditor are deferred at approval) stops with `task_deferred`.

The CLI freezes `orcexe.contract.lite.v1` as a private (0600) file at
`.orcexe/runs/<dispatch-id>/contract.lite.json`, outside the executor checkout.
It binds task/plan IDs, goal, allowed globs, done criteria, three attempts, lane
and the integration base; `contract.frozen` records its SHA-256 before the executor starts.
Every later transition reopens and verifies this contract. The brief contains
fenced contract data, fenced untrusted task notes, the previous gate feedback,
and rules to stay in scope and add or update tests for every done criterion.

Attempts share one managed worktree. The gate uses gate_lite's raw Git snapshot,
control checks and isolated project-check copy, with the same shared glob grammar
for `files.allow`. Fast tasks retain the single-file, comments/docs gate.
Standard tasks allow multiple source/test edits, additions and deletions, checked
for whitespace/conflicts, secrets and test-floor regressions. Git metadata,
symlinks/submodules and edits to command sources remain unsupported. Commands
come from the pre-executor base; project checks run on the immutable candidate
snapshot in their own copy. An out-of-scope path ends the run as `rejected`,
records `lane.escalated` with paths and a standard/risky suggestion, and never
widens the contract. After the allow-list check and before any gate/L0/L1 checks,
every attempt in every lane checks all changed paths (including old names and
intermediate versions) against the shared risk areas: built-in paths, local
`risk-map.yaml`, project-map auth/payment/data/migration entries and
`do_not_touch`. A match is terminal `rejected` with `risk_path_touched`, exit 1,
`gate.failed` and a risky `lane.escalated` suggestion listing the matching paths.
It is never retried or accepted. On the risky lane only the contract's frozen
`forbidden_paths` give `risk_path_touched` (see below).
Other gate failures give feedback and retry, up to three
attempts, then end as `exhausted`. Escaped processes (`pipes_held`) and changed
controls/main repository stop the run.

An executor run whose output exceeded orcexe's line cap (run warning
`output_capped`: a cut line loses or garbles its event) or whose actual model
differs from the routed one (`model_mismatch`: a different model's work is not the
contract's work) never counts as a clean completion (T61): in every lane the
attempt fails `run_status` with that warning as its detail (a real failure of the
attempt, not quota), `do` ends `failed` with `failed_check: run_status`, and an L1
or auditor receipt with either warning is unavailable.

Every gate checks the cumulative worktree against the task base, not just the
latest attempt's edits. A retry that finds the work already done and edits nothing
(the executor reports `empty_diff`) is therefore gated on that tree: scope, text,
secrets, test floor, project checks and L0/L1 run as for the first attempt. A first
attempt with `empty_diff` still fails `run_status`; a tree that never differed from
the base fails `diff_collect`; a retry whose own edits break the tree fails. When the
previous attempt failed in project checks, the feedback names the check and its
detail and asks the executor to re-check the current files rather than rewrite them.
A timed-out project check reports its limit, for example `unittest: timeout (600s)`.
`do` has a single attempt and keeps failing `empty_diff` at `run_status`.

Every code-executing project step, including candidate checks, L0 base counts and
red-first reruns, uses a fresh sandbox and temp directory. On macOS, Seatbelt
(`/usr/bin/sandbox-exec`) allows writes only to that checkout, the step temp and
runner device files, and denies outbound network. Profile paths are canonical and
escaped. Linux uses `bwrap` with a read-only root, writable checkout/temp and private
network. Processes a step forks or execs inherit the policy; symlinks cannot grant
outside writes. On macOS the profile also denies LaunchServices opens (`lsopen`,
for example `/usr/bin/open -a App`) and sending Apple events (`appleevent-send`),
because an app started that way runs outside the sandbox; the rest of the profile
is `(allow default)`, so other mach/XPC services stay reachable. On Linux, `bwrap`
unshares only the network namespace; other IPC with the host is not restricted.
A narrower IPC policy (a macOS mach-lookup allow-list, `bwrap --unshare-all`) is
planned for v0.3. `TMPDIR`/`TEMP`/`TMP` point to the fresh step temp. A denied write fails the
project check normally, with `write outside checkout denied` in feedback.

If a backend is missing or the host refuses sandbox creation, steps still run, but
`task` can only reach `gate_passed` with `isolation_unavailable` (exit 0), even after
L1 success. `do` returns `unverified` with the same reason (exit 1), without landing
advice. `ORCEXE_NO_SANDBOX=1` injects this path for testing. Existing gate ledger
events record `isolation: seatbelt|bwrap|none` per attempt. Before and after every
code step the CLI snapshots the main repository's `.orcexe/` controls, including
ledger, plan, approvals, contracts and status. Any modification is terminal
`rejected`/`state_tampered` (exit 1; `do`: `failed`). A persistent
`.orcexe/state-tampered` marker prevents later dependency acceptance; investigate
and restore trusted state before removing it. Dependency reads refuse marked state
with `state_tampered` and verify the entire ledger (see the ledger section below).

The same rule covers the time an executor or L1 evaluator runs (T55). Before and
after every external model run the CLI observes the ledger bytes, its head ref and
a fingerprint of every `.orcexe/runs/` directory. A ledger record, a moved ref or
any file under `runs/` not written by the CLI or the adapter's fixed artifacts
(`brief.md`, `final.md`, `events.jsonl`, `stderr.txt`, `result.json`, `.attempt`,
`brief/brief.md` in that call's own folder) is terminal `state_tampered`; `plan --run`
stops there (exit 2), so no dependant of a forged acceptance is dispatched. During an
L1 call the main-repository snapshot of the executor window (config, `plan.json`,
`decisions.jsonl`, refs, status) is compared too: a change is
`main_repository_changed`. Decisions never re-read run artifacts from disk: the L1
verdict is parsed from the final text the adapter captured in memory when the run
ended, and L0 runner counts from the gate's in-process log copy, so a process the
executor left behind cannot rewrite them. `do` applies the same checks to its
executor.

Only the CLI writes task lifecycle ledger events:
`contract.frozen` → `attempt.started` → `attempt.ended` →
`gate.passed|gate.failed` → final `task.state`. Worktree status files and agent
self-reports cannot approve a task. After project checks pass, standard L0 checks
static discoverable test definitions and the final unittest/pytest runtime summary
against base (`test_count_dropped`; unknown runtime counts stay `not_seen`). Python
uses AST definitions in `test*.py`/`*_test.py`; JS/TS conservatively counts `it`/`test`
calls in `.test.*`/`.spec.*` files. Added skip/focus markers, `SkipTest`,
`pytest.skip`/`xfail`/`importorskip`, expected-failure decorators, hollow tests
(no check, or an immediate `return`) and increased runtime skipped/xfailed/
expected-failure counts fail `skip_added`. New or changed tests without assertions
fail `assertion_missing`. **L0 test commands:** project checks in `commands.yaml`
count as test commands when the command `id` and `argv` together match
`test`, `tests`, `pytest`, or `unittest` at word boundaries (regex
`(?:^|[-_/ .])(?:test|tests|pytest|unittest)(?:$|[-_/ .])` over
`id + ' ' + joined argv`). Only those commands supply red-first and
`test_count_dropped` runtime evidence; name checks accordingly (for example
`id: unit-tests` with `argv: [python3, -m, pytest]`). L0 shares slop's check detector: Python `assert`, calls
starting with `assert`/`expect`/`should`/`verify` on any receiver, `fail` calls,
exception-check contexts, and `raise`/`throw` in the test body count as checks.
This includes Deno std assertions, XCTest `XCTAssert*`/`XCTFail` and Swift
`#expect`/`#require`. Uncalled nested declarations and literal/comment text do
not supply checks. Non-documentation edits require changed tests. Red-first
proof overlays changed/added tests on a fresh base copy that must fail. Gate and
red-first checkouts have the same absolute path shape, depth and `checkout` basename,
with no `red`/`base` path markers. Importing a new, not-yet-existing symbol counts as
red; this is accepted, known-weak evidence and does not prove behavioral coverage. L0 and L1
failures share the three-attempt budget and supply feedback to the next attempt.

L1 routes an evaluator from one access snapshot, denying the executor's model,
preferring another family, and excluding Cursor. Claude uses `manager`; Codex
uses `review` (`-s read-only`), in a fresh candidate copy. It returns strict
`orcexe.rubric.v1`: exact ordered `d1..dN` IDs for the done criteria, boolean
pass values, bounded reasons and `overall_pass == all(pass)`. The evaluator output
is parsed strictly, unlike manager output: either the whole reply is one bare JSON
object, or it is exactly one ```` ```json ```` fence, optionally after plain prose that
holds no `{` or `}` and no fence-like line (```` ``` ```` or `~~~` after indentation or
`>` blockquote markers), with only whitespace after the closing fence. Anything else
is `l1_invalid`, so a rubric block quoted from the diff cannot stand in for the
verdict. The CLI accepts
only when every criterion passes. Missing access/runtime failure leaves
`gate_passed` with `l1_unavailable`; invalid output leaves `gate_passed` with
`l1_invalid`, without retries. Ledger `l0.passed|l0.failed` events include check
IDs; `l1.started` and `l1.verdict` record the evaluator model and criterion
pass/fail, without evaluator prose. Fast success is `accepted` only with isolation. Successful or
gate-passed runs exit 0 and print a
human command for a frozen land ref. Landing stays manual; inspect that commit
before running the command. Runs and completed executor worktrees are retained; interrupted worktrees are removed.

`--json` prints `orcexe.task.v1` with task/plan IDs, final state, nullable reason,
attempt count, nullable contract SHA-256, nullable escalation (`suggested_lane`,
`paths`) and nullable land (`ref`, `command`). Precondition failures have state
`blocked`, zero attempts and no contract. Human output prints the final state;
exit-2 diagnostics also appear on stderr as `not_seen: <reason>`.
`--timeout-min` is a positive, finite per-attempt executor budget (default 30).

<!-- task-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `accepted` | 0 | The CLI accepted the frozen candidate. |
| `audit_invalid` | 0 | Risky lane: the auditor reply was invalid twice (strict `orcexe.audit.v1`); not landable. |
| `auditor_unavailable` | 0 | Risky lane: no eligible cross-family auditor (run-time family), it failed, or its call budget is spent; no ref, no landing command. |
| `auditor_wrote` | 1 | Risky lane: the read-only auditor changed its own throwaway copy. |
| `budget` | 1 | `plan --run` with `--max-calls`/`--max-minutes`: the next model call was refused; the task stays interrupted for `--resume`. |
| `cancelled` | 130 | Task execution was interrupted. |
| `contract_tampered` | 1 | The frozen contract changed after dispatch. |
| `contract_unapproved` | 2 | Risky lane: the contract proposal awaits the user's `task --approve-contract` (or the hash is not the current proposal's, `detail: proposal_changed`). |
| `dependency_pending` | 2 | A dependency has not been accepted. |
| `exhausted` | 1 | All three attempts were consumed. |
| `gate_passed` | 0 | Mechanical gates passed; full acceptance is unverified. |
| `git_controls_changed` | 1 | Git control state changed during execution. |
| `git_identity_missing` | 1 | Configure Git author and committer identity. |
| `internal_error` | 70 | Internal computation failed; inspect the reported step. |
| `isolation_unavailable` | 0 | No usable OS sandbox; acceptance is withheld. |
| `l1_basis_insufficient` | 0 | Risky lane: only a same-family or Cursor read-only L1 was available; `cross_family` is required. |
| `l1_invalid` | 0 | The evaluator rubric was invalid. |
| `l1_unavailable` | 0 | An independent evaluator could not complete. |
| `lane_not_supported` | 2 | This entry point cannot execute the selected lane. |
| `ledger_corrupt` | 2 | The ledger has an invalid line (often a torn tail); run `orcexe ledger repair`. |
| `ledger_head_mismatch` | 2 | The ledger tail differs from its anchored head ref; run `orcexe ledger verify`, then `orcexe ledger repair`. |
| `main_repository_changed` | 1 | The executor changed the main repository. |
| `no_route` | 2 | The task has no eligible saved executor route. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `observation_unavailable` | 1 | Required safety evidence could not be observed. |
| `orcexe_invalid` | 2 | `.orcexe` at the repository root is a symlink or not a directory; remove or rename it. |
| `output_symlink` | 2 | An output path is an unexpected symlink. |
| `outside_allow` | 1 | A changed path is outside the frozen allowed scope. |
| `pipes_held` | 1 | An escaped process may still hold the executor pipes. |
| `plan_mismatch` | 2 | The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | No saved plan exists. |
| `plan_not_approved` | 2 | Approve this plan before executing it; after `approval.revoked` (`detail: approval_revoked`) approve it again. |
| `plan_stale` | 2 | The plan was made for another HEAD, its classifier or runnable lanes changed (`detail: classifier_changed`/`lanes_changed`), or it was approved before partial approval. |
| `plan_tampered` | 2 | The plan hash, approval lists, reasons, decision record or task shape is invalid. |
| `risk_path_touched` | 1 | A changed path matches a protected risk area. |
| `run_interrupted` | 2 | Use --resume to continue an unfinished plan. |
| `same_failure` | 1 | Risky lane: the same failure class twice; task is `diagnosing`, no third attempt, not landable. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `state_tampered` | 1 | Project code, a run or a leftover process changed CLI control state (see `orcexe ledger repair` recovery), or a plan decision has no ledger anchor (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`; propose again). |
| `task_deferred` | 2 | The task is deferred by the latest plan decision; open it with `plan --approve PLAN_ID --open`. |
| `task_running` | 1 | Another dispatch holds the repository lock. |
| `task_unknown` | 2 | The task ID is absent from the approved plan. |
<!-- /task-exit-reasons -->

Failure reasons above appear on stderr as `not_seen: <reason>`. Map, questions
and plan distinguish validation, computation and publication failures. Internal
computation failures exit 70 with `not_seen: internal_error` and
`detail: <step> <exception class>`; exception contents are never printed.

`orcexe questions [--repo PATH] [--goal TEXT] [--json]` reads a map at the
current HEAD and writes a private, atomic `.orcexe/form.json`. Selection is
offline and deterministic: at most four questions per goal across forms, with evidence-backed,
previously decided, low-impact, and excess candidates listed as assumptions.
With `--goal`, unknown candidates must name a path or module matching a goal term
or goal path; prose-only overlap does not count. Unrelated unknowns become
`not_relevant` assumptions. Short stopwords and tokens occurring in more than half
of tracked paths cannot establish term overlap. Conflicts and goal-scope questions
remain eligible. Risk and do-not-touch confirmations become `deferred_to_plan`
assumptions, covered by the single plan approval after task paths are known.
Without a goal, question selection retains all candidate kinds and the cap is four
per form. Answers are global by question ID: a decided question is never asked
again for any goal. Answer records carry a goal hash normalized for case,
whitespace and trailing punctuation; each normalized goal has its own four-answer budget.
After four answers, remaining candidates become `over_limit` assumptions and planning proceeds.
Direction conflicts come from the map's structured `direction_sentences`.
The small goal heuristic treats “so that”, “until”, “when”, “must”, “should”,
“tests pass” (whole phrases), or any digit as a completion criterion.
Missing/stale maps exit 2 with `not_seen: map_missing` / `not_seen: map_stale`.

`orcexe answer [--repo PATH] --form FORM_ID --choice QID=INDEX|LABEL --text QID=TEXT`
accepts repeated choice/text flags and optional `--json`. A choice may be the
option's exact label or its zero-based index; both forms record the same
decision. An all-digit value that is in range is read as an index, even if it
equals another option's label; any other value must exactly match an option
label. The entire batch must reference the current form and HEAD and contain
valid, distinct question IDs before any records append to
`.orcexe/decisions.jsonl`. An unmatched choice for a valid, unanswered question
exits 2, prints `not_seen: answer_invalid`, then `valid choices for QID:` and one
`  INDEX: LABEL` line per option on stderr, and appends nothing. Other invalid
batches (unknown or repeated question ID, empty text, already decided) print
only `not_seen: answer_invalid`. Recorded questions become assumptions on later forms,
for every goal, including after HEAD changes and a map refresh. The form hash is
recomputed before answering; modified forms report `form_tampered`. A question
ID can be answered once. Answers empty after cleaning are invalid; the batch
appends nothing. Free text is always allowed;
displayed and recorded prose is redacted and capped at 4,000 characters.

`orcexe history [--repo PATH] [--json] [--issues]` writes private, atomic
`.orcexe/intake/u2.json` (history) and `u3.json` (direction sources) in the main
worktree. `--json` prints both documents. History includes weekly phases over
180 days, hotspots over 90 days, reverts, branch divergence and author counts;
windows use HEAD's UTC committer date. Direction sources contain committed
roadmaps, TODOs, ADRs, decisions, only the unreleased CHANGELOG section, and the
20 newest non-merge commits. Dirty files are ignored. Secret and generated paths
are excluded, identities and emails are redacted, and doc text is capped at
4000 characters. Shallow and empty history are explicitly marked unseen.

The command runs no models or project commands. Only `--issues` can access the
network: with a GitHub remote it runs a bounded `gh issue list`, retaining only
titles and label names. Set `ORCEXE_BIN_GH` to override the binary. Missing auth,
binary, remote, errors and timeouts produce explicit `not_seen` reasons.
Success (including empty history) exits 0; a non-repository exits 2.

## Offline source signals

`orcexe slop [--repo PATH] [--json]` scans committed HEAD source without models,
network access, or executing project code. It writes atomic, private (0600)
`.orcexe/slop.json` and `.orcexe/slop-report.md` in the main worktree; `--json`
prints the saved JSON. Findings do not fail the command (exit 0); a non-repository
exits 2. Dirty and untracked source is ignored. Intake's secret-path/blob guards,
vendored-tree and state exclusions, 256 KiB file cap, 4 MiB fetch budget, redaction,
command candidates and CI evidence also apply here. Slop, the gate and L0 parse
scanned Python with SyntaxWarning and DeprecationWarning suppressed; findings and
results do not change.

The seven classes are fake tests, skipped/focused tests, checker suppressions,
unimplemented bodies, advisory unused public exports, dependency mismatches and
missing test/lint/CI checks. Python and JS/TS support all classes; Swift supports
fake tests, skips, suppressions and stubs. Asserting test helpers are followed up to three
calls through local definitions and imported test-support source; product-only
calls still need assertions. Conditional platform/environment/dependency skips
remain visible with `advisory: true` and `signal: "conditional_skip"`; unconditional
skips, constant conditions, xfail and focus markers stay scored. Counts include
advisory findings. Constant-only assertions and known product-only assertion-like
calls retain their slop precision signals; L0 checks whether assertion syntax is
present. Dead exports exclude names used within their own file. Python scripts
(shebang, main guard, `scripts/`/`bin/`/`tools/`, or unimported files outside a
package) do not export. Unused public names in imported modules and packages
remain advisory findings.

Deno `jsr:`, `npm:`, `node:` and HTTP(S) specifiers declare their dependencies;
`imports` mappings in `deno.json` and `import_map.json` also declare exact names
and slash-terminated prefixes. Python module files anywhere in the HEAD inventory
are local, including modules reached through literal `sys.path.insert`/`append`
directories. Python declarations across manifests and supported locks
(`Pipfile.lock`, `poetry.lock`, `uv.lock`) and Node package/shrinkwrap locks are
recognized without executing code. Undeclared third-party imports still produce
findings. Other extensions, capped files and unsupported
syntax are explicit in `unseen`. Detection is syntactic: the unused-export name
search is advisory, dynamic imports and computed exports are not resolved, and
template literals are opaque. The Markdown report groups findings and suggests
`fast` or `standard` fix lanes; it makes no pass/fail decision.

## Development

From the repository root, run `python3 tests/tools/run_tests.py`. The stdlib-only
runner discovers the same `test*.py` modules as `python3 -m unittest discover`,
runs each in a subprocess with a private home and temp directory, and defaults
to one worker per CPU. Use `-j 1` for serial module execution; ordinary unittest
discovery also remains supported. Each module has a 300-second timeout (1800
seconds in slow mode), multiplied by `ORCEXE_TEST_TIME_SCALE` (`--timeout` overrides the base). Previous module
durations in the ignored `tests/.durations.json` schedule longer modules first.

The default suite targets at most 60 seconds on a quiet development machine and
prints its measured wall total, test/module counts, failures, errors and skips;
time is reported, never asserted. Fast examples cover each behavior, including
all v0.2 security paths and deterministic validator/complexity tests. Extended
manager/planner publication matrices, cross-interpreter layouts, task race and
stacked retry scenarios live in `tests/slow/`. Generated stress sets and timing
checks also remain opt-in.

Run the full suite with `python3 tests/tools/run_tests.py --slow` or equivalently
`ORCEXE_SLOW_TESTS=1 python3 tests/tools/run_tests.py`. Both include the default
suite. Slow modules default to a 1800-second timeout; override it with `--timeout`.
Run slow coverage for changes to orchestration, adapters, gates, publication,
redaction, classification or YAML parsing. Ordinary unittest discovery honors
`ORCEXE_SLOW_TESTS=1` too. No real model CLI, network or install is needed.

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


### `map` diagnostic reasons

<!-- map-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `gitlink_tree` | 2 | The tree contains unsupported submodules. |
| `head_moved` | 2 | HEAD changed while observations were being collected. |
| `internal_error` | 70 | Internal computation failed; inspect the reported step. |
| `manager_output_invalid` | 2 | Manager output failed the closed schema or bounds. |
| `manager_tool_not_read_only` | 2 | Only Claude with the manager profile is allowed. |
| `manager_unavailable` | 2 | The manager could not complete. |
| `manager_unavailable: auth` | 2 | Manager failed: auth. |
| `manager_unavailable: failed` | 2 | Manager failed: failed. |
| `manager_unavailable: no_route` | 2 | Manager failed: no_route. |
| `manager_unavailable: quota` | 2 | Manager failed: quota. |
| `manager_unavailable: stalled` | 2 | Manager failed: stalled. |
| `manager_unavailable: timeout` | 2 | Manager failed: timeout. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `output_symlink` | 2 | An output path is an unexpected symlink. |
| `orcexe_invalid` | 2 | `.orcexe` at the repository root is a symlink or not a directory; remove or rename it. |
| `state_dir_in_repository` | 2 | The resolved `ORCEXE_STATE_DIR` is inside the repository or its main worktree. |
| `write_failed` | 2 | Publishing private state failed. |
<!-- /map-exit-reasons -->


### `questions` diagnostic reasons

<!-- questions-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `internal_error` | 70 | Internal computation failed; inspect the reported step. |
| `map_missing` | 2 | Generate a project map first. |
| `map_stale` | 2 | Refresh the map at the current HEAD. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `output_symlink` | 2 | An output path is an unexpected symlink. |
| `orcexe_invalid` | 2 | `.orcexe` at the repository root is a symlink or not a directory; remove or rename it. |
| `questions_invalid` | 2 | The map, goal or question data is invalid. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `write_failed` | 2 | Publishing private state failed. |
<!-- /questions-exit-reasons -->


### `answer` diagnostic reasons

<!-- answer-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `answer_invalid` | 2 | The answer batch does not match the current form. |
| `form_tampered` | 2 | The saved form content differs from its hash. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
<!-- /answer-exit-reasons -->


### `do` diagnostic reasons

<!-- do-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `acceptance_failed` | 1 | Creating the frozen acceptance commit failed. |
| `code_unchanged` | 1 | The edit changes executable code. |
| `controls` | 1 | Git controls could not be verified. |
| `diff_check` | 1 | Whitespace or conflict checks failed. |
| `diff_collect` | 1 | The complete change set could not be observed. |
| `gate_accepted` | 0 | All fast gates passed with isolation. |
| `git_identity_missing` | 1 | Configure Git author and committer identity. |
| `internal_error` | 70 | Internal computation failed; inspect the reported step. |
| `isolation_unavailable` | 1 | No usable OS sandbox; acceptance is withheld. |
| `lane_risky` | 7 | Risky execution is not supported. |
| `lane_standard` | 7 | Use an approved standard-lane plan. |
| `ledger_corrupt` | 2 | The ledger has an invalid line (often a torn tail); run `orcexe ledger repair`. |
| `ledger_head_mismatch` | 2 | The ledger tail differs from its anchored head ref; run `orcexe ledger verify`, then `orcexe ledger repair`. |
| `main_repository_changed` | 1 | The executor changed the main repository. |
| `pipes_held` | 1 | An escaped process may still hold the executor pipes. |
| `project_checks` | 1 | Project checks failed or were not observed. |
| `report_failed` | 70 | Writing the final report failed. |
| `route_forced_unavailable` | 1 | No eligible route (forced unavailable); mixed or other access failures. |
| `route_forced_unavailable` | 4 | No eligible route (forced unavailable); all candidates lack authentication. |
| `route_forced_unavailable` | 5 | No eligible route (forced unavailable); all candidates exhausted quota. |
| `route_no_eligible_candidate` | 1 | No eligible route (no eligible candidate); mixed or other access failures. |
| `route_no_eligible_candidate` | 4 | No eligible route (no eligible candidate); all candidates lack authentication. |
| `route_no_eligible_candidate` | 5 | No eligible route (no eligible candidate); all candidates exhausted quota. |
| `route_no_prior` | 1 | No eligible route (no prior); mixed or other access failures. |
| `run_status` | 1 | Executor runtime failed or completion could not be verified. |
| `run_status` | 4 | Executor runtime failed or completion could not be verified. |
| `run_status` | 5 | Executor runtime failed or completion could not be verified. |
| `run_status` | 6 | Executor runtime failed or completion could not be verified. |
| `scope` | 1 | Changes exceeded the allowed target. |
| `secrets` | 1 | Secret scanning failed. |
| `signal_SIGHUP` | 1 | Cancelled by SIGHUP; do retains its failure exit code. |
| `signal_SIGINT` | 1 | Cancelled by SIGINT; do retains its failure exit code. |
| `signal_SIGTERM` | 1 | Cancelled by SIGTERM; do retains its failure exit code. |
| `size` | 1 | Changes exceeded the fast-lane budget. |
| `state_tampered` | 1 | Project code, a run or a leftover process changed CLI control state (see `orcexe ledger repair` recovery), or a plan decision has no ledger anchor (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`; propose again). |
| `test_floor` | 1 | Tests were removed or skip markers were added. |
<!-- /do-exit-reasons -->


### `status` diagnostic reasons

<!-- status-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `ledger_corrupt` | 2 | The ledger has an invalid line (often a torn tail); run `orcexe ledger repair`. |
| `ledger_head_mismatch` | 2 | The ledger tail differs from its anchored head ref; run `orcexe ledger verify`, then `orcexe ledger repair`. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `orcexe_invalid` | 2 | `.orcexe` at the repository root is a symlink or not a directory; remove or rename it. |
| `plan_missing` | 2 | No saved plan exists. |
| `plan_tampered` | 2 | The plan hash, approval lists, reasons, decision record or task shape is invalid. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `state_tampered` | 2 | Project code, a run or a leftover process changed CLI control state (see `orcexe ledger repair` recovery), or a plan decision has no ledger anchor (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`; propose again). |
<!-- /status-exit-reasons -->

Diagnostic tables list reason/result codes for each command. For `plan --run`,
per-task reasons appear inside `tasks`; the command itself exits 0 for `completed`,
1 for `partial`, 130 for interruption, 2 for preconditions, or 70 for internal
errors. `status` reports recorded task reasons without changing their meaning;
a successful read exits 0 regardless of those task states. `do` can report the
same route reason with different codes according to all-auth, all-quota or mixed
access failures. Argument
parser errors exit 2 without a structured reason. Map/plan signals return
`128 + signal` without a diagnostic reason; task/plan-run interruption uses 130.
