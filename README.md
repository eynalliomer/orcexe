# orcexe

orcexe is an open-source Python CLI and Claude Code plugin that orchestrates AI coding tools.
It uses only the Python standard library and supports Python 3.9 and later.
orcexe has no telemetry.

Status: **v0.1.0 walking skeleton** (local tag, not published). See `CHANGELOG.md` for known gaps.

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
checkout and their temporary scratch/HOME. Descendants inherit the policy.
On other systems, or when sandbox creation is denied, steps still run with a
temp HOME but without OS confinement: `isolation_unavailable` prevents acceptance
(`task`: `gate_passed`, exit 0; `do`: `unverified`, exit 1). The reports record
`seatbelt`, `bwrap` or `none`; do not interpret an exit of zero alone as acceptance.

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
| `${ORCEXE_STATE_DIR:-~/.cache/orcexe}/` | `env.json` readiness cache, manager run evidence under `runs/`, Claude runtime files and disposable `gate/` checkouts. Set `ORCEXE_STATE_DIR` to an absolute path outside the repository to relocate state; manager runs reject a state dir inside the repository. |
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
and the worktree is always kept. Standard tasks use an approved `plan` and the `task` command; risky tasks are deferred. Each tool uses your existing login; `do` does not log in or install tools.

Options: `--repo` defaults to cwd; `--timeout` defaults to 1800 seconds; `--json`
prints one redacted summary. `--model-constraint` accepts a JSON route constraint
object (`tools`, `deny_models`, `exclude_families`, or `force` with tool/model/effort).
`--dry-run` classifies and probes access/routes without a model call or any project
write: no lock, ledger event, worktree, or brief.

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
Non-repositories, gitlink-containing HEAD trees and a symlinked `.orcexe` report
`not_seen: not_a_repository`, `not_seen: gitlink_tree` and `not_seen: output_symlink`,
respectively. SIGINT/SIGTERM/SIGHUP/SIGQUIT stop the manager process group, remove its copy and
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
One access snapshot routes every task through the
`executor` stage. No eligible executor leaves `route.tool: null` and its reason;
it does not reject the plan. Routes include tool, model, effort, confidence and basis.

Success publishes private (0600) `.orcexe/plan.json` (`orcexe.plan.v1`) and
`plan.md` together, preserving the map. The single form shows the goal,
assumptions, each task's allowed files, size, parallel flag, lane, tool/model, dependencies and done criteria, and the
approval command. A plan starts as `proposed` and records HEAD and decision IDs.

`orcexe plan [--repo PATH] --approve PLAN_ID [--json]` requires that exact proposed
plan at the same HEAD. It marks it `approved`, records `approved_at`, and appends
one `orcexe.decision.v1` record of kind `plan_approval` to `decisions.jsonl`.
`content_sha256` binds the canonical JSON content (sorted keys, compact separators,
UTF-8), excluding `plan_id`, `content_sha256`, `status`, `approved_at` and `approval`.
`plan_id` is `plan-` plus this digest. Approval recomputes it, rechecks cleaned
tasks, P3 and current lanes, rejects empty or changed plans as `plan_tampered`,
and records the digest in the approval decision. Later consumers can call
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
| `plan_mismatch` | 2 | The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | No saved plan exists. |
| `plan_not_proposed` | 2 | Only a proposed plan can be approved. |
| `plan_rejected` | 2 | Deterministic P3 checks rejected the proposed tasks. |
| `plan_stale` | 2 | The plan was made for another HEAD. |
| `plan_tampered` | 2 | The plan hash, approval or task shape is invalid. |
| `proposed` | 0 | A plan is ready for human approval. |
| `questions_invalid` | 2 | The map, goal or question data is invalid. |
| `questions_pending` | 2 | Answer the saved form before planning. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `write_failed` | 2 | Publishing private state failed. |
<!-- /plan-exit-reasons -->

## `plan --run`, `--resume` and `orcexe status`

```sh
orcexe plan --run PLAN_ID [--resume] [--json] [--repo PATH]
orcexe status [--repo PATH] [--json]
```

`plan --run` validates approval, the canonical hash, tampering and HEAD once
under the existing repository dispatch lock. HEAD must still equal the approved
plan head (`plan_stale` otherwise); landing part of the plan does not relax this
rule. Tasks run sequentially in topological order, choosing plan order among
ready tasks. It reuses `task` for the contract, attempts, L0 and L1 gates.
Only ledger state `accepted` satisfies a dependency. An `exhausted`, `rejected`
or `gate_passed` task blocks all transitive dependents with `dependency_failed`;
independent tasks continue. Risky tasks are `blocked`/`lane_not_supported`, and
their dependents are blocked too.

The CLI keeps one integration chain at `refs/orcexe/plans/<plan-id>`, starting
at the approved plan head. Every task, including a single `orcexe task`, starts
from the current chain tip, which is frozen in `contract.lite.head`. An accepted
result advances the chain with a frozen commit parented by the previous tip.
Gate-passed results do not advance it. HEAD, the user's index and working files
stay untouched. This ref is never a branch; landing stays manual. Review the
chain and use the printed `git -C … merge --ff-only refs/orcexe/plans/…` command
while HEAD equals the plan head.

SIGINT/SIGTERM closes the partial attempt in the ledger, removes its worktree
and exits 130 with state `interrupted`. Without `--resume`, an interrupted plan
refuses with exit 2 and `run_interrupted`. With `--resume`, final tasks are
skipped and the interrupted task starts a fresh attempt within its original
three-attempt budget, using the same frozen contract and SHA-256. Interrupted
attempts consume budget. A finished plan re-run does nothing and returns the
same final outcome. Only one plan/task/do dispatch can run per repository.

`--json` returns `orcexe.planrun.v1` with `plan_id`, `state`
(`completed|partial|interrupted`), `tasks` and nullable `land` (`ref`, `command`).
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
| `no_route` | 2 | The task has no eligible saved executor route. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `output_symlink` | 2 | An output path is an unexpected symlink. |
| `partial` | 1 | At least one task was not accepted. |
| `plan_mismatch` | 2 | The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | No saved plan exists. |
| `plan_not_approved` | 2 | Approve this plan before executing it. |
| `plan_stale` | 2 | The plan was made for another HEAD. |
| `plan_tampered` | 2 | The plan hash, approval or task shape is invalid. |
| `run_interrupted` | 2 | Use --resume to continue an unfinished plan. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `state_tampered` | 2 | Project code changed CLI control state; investigate before retrying. |
| `task_running` | 2 | Another dispatch holds the repository lock. |
<!-- /planrun-exit-reasons -->

## `task`: execute one approved task

```sh
orcexe task --plan PLAN_ID --task T1 --repo /path/to/repo --json --timeout-min 30
```

`task` verifies the saved plan's ID, approval, HEAD and canonical content hash
against its approval decision before creating a worktree or dispatching a tool.
Dependencies must have ledger state `accepted`. It uses the
approved task's tool/model/effort route; risky tasks and unassigned routes stop.

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
It is never retried or accepted. Already-risky plans remain `lane_not_supported`.
Other gate failures give feedback and retry, up to three
attempts, then end as `exhausted`. Escaped processes (`pipes_held`) and changed
controls/main repository stop the run.

Every code-executing project step, including candidate checks, L0 base counts and
red-first reruns, uses a fresh sandbox and temp directory. On macOS, Seatbelt
(`/usr/bin/sandbox-exec`) allows writes only to that checkout, the step temp and
runner device files, and denies outbound network. Profile paths are canonical and
escaped. Linux uses `bwrap` with a read-only root, writable checkout/temp and private
network. Children and daemons inherit the policy; symlinks cannot grant outside
writes. `TMPDIR`/`TEMP`/`TMP` point to the fresh step temp. A denied write fails the
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
and restore trusted state before removing it. Dependency reads verify the entire
ledger and refuse corrupted or marked state with `state_tampered`.

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
fail `assertion_missing`. L0 shares slop's check detector: Python `assert`, calls
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
pass values, bounded reasons and `overall_pass == all(pass)`. The CLI accepts
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
| `cancelled` | 130 | Task execution was interrupted. |
| `contract_tampered` | 1 | The frozen contract changed after dispatch. |
| `dependency_pending` | 2 | A dependency has not been accepted. |
| `exhausted` | 1 | All three attempts were consumed. |
| `gate_passed` | 0 | Mechanical gates passed; full acceptance is unverified. |
| `git_controls_changed` | 1 | Git control state changed during execution. |
| `git_identity_missing` | 1 | Configure Git author and committer identity. |
| `internal_error` | 70 | Internal computation failed; inspect the reported step. |
| `isolation_unavailable` | 0 | No usable OS sandbox; acceptance is withheld. |
| `l1_invalid` | 0 | The evaluator rubric was invalid. |
| `l1_unavailable` | 0 | An independent evaluator could not complete. |
| `lane_not_supported` | 2 | This entry point cannot execute the selected lane. |
| `main_repository_changed` | 1 | The executor changed the main repository. |
| `no_route` | 2 | The task has no eligible saved executor route. |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `observation_unavailable` | 1 | Required safety evidence could not be observed. |
| `output_symlink` | 2 | An output path is an unexpected symlink. |
| `outside_allow` | 1 | A changed path is outside the frozen allowed scope. |
| `pipes_held` | 1 | An escaped process may still hold the executor pipes. |
| `plan_mismatch` | 2 | The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | No saved plan exists. |
| `plan_not_approved` | 2 | Approve this plan before executing it. |
| `plan_stale` | 2 | The plan was made for another HEAD. |
| `plan_tampered` | 2 | The plan hash, approval or task shape is invalid. |
| `risk_path_touched` | 1 | A changed path matches a protected risk area. |
| `run_interrupted` | 2 | Use --resume to continue an unfinished plan. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
| `state_tampered` | 1 | Project code changed CLI control state; investigate before retrying. |
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

`orcexe answer [--repo PATH] --form FORM_ID --choice QID=INDEX --text QID=TEXT`
accepts repeated choice/text flags and optional `--json`. Choice indexes start
at zero. The entire batch must reference the current form and HEAD and contain
valid, distinct question IDs before any records append to
`.orcexe/decisions.jsonl`. Recorded questions become assumptions on later forms,
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
command candidates and CI evidence also apply here.

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
| `state_tampered` | 1 | Project code changed CLI control state; investigate before retrying. |
| `test_floor` | 1 | Tests were removed or skip markers were added. |
<!-- /do-exit-reasons -->


### `status` diagnostic reasons

<!-- status-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `not_a_repository` | 2 | The path is not a supported Git repository. |
| `output_symlink` | 2 | An output path is an unexpected symlink. |
| `plan_missing` | 2 | No saved plan exists. |
| `plan_tampered` | 2 | The plan hash, approval or task shape is invalid. |
| `state_corrupt` | 2 | Saved state or ledger is unreadable or inconsistent. |
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
