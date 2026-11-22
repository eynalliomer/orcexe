## Unreleased

### T49 — test hygiene: no repo-root temp dirs, load-tolerant probes, stale slow fixture

- Temp trees: `test_do`, `test_trust`, `test_fake_claude`, `test_fake_agent` (was under
  `tests/`), `test_doctor`, `test_cli` (3x), `test_route` (2x) and `test_hosts` created
  them with `dir=ROOT`, so a cleanup that did not run under load left `orcexe-do-*` and
  similar directories in the checkout. They now use the default temp location (the
  runner's per-module `TMPDIR`). No site depended on the repository location: paths that
  are compared are already `.resolve()`d or derived from the same temp path, nested git
  repositories have their own `.git`, and every module passes with the macOS
  `/var/folders` temp (symlinked to `/private/var`) as well as under the runner.
  `tests/unit/test_test_runner.py` statically rejects any `tempfile` call in the tests
  that names `ROOT`; only the runner keeps its own directory (which it removes) there.
- Load-sensitive deadlines: a fake `--version` probe once exceeded `detect_timeout_s=3`
  under suite load and `detect()` returned version None. Steps a test needs to finish
  (version/auth/model probes, fake runs expected to complete, the fast-lane gate, hang
  guards around CLI subprocesses) now use `tests.unit._timing.roomy()` (30 s, scaled)
  in `test_adapter_contract`, `test_cursor_adapter`, `test_do` and `test_run_fake`. The
  exact version/path/min_ok assertions are unchanged; the short timeout/stall/kill-grace
  values that are the behavior under test stay short.
- `tests/slow/test_mapping.py`: the large-repo fixture's `src/exports.py` was imported
  by nothing, and since T47 slop skips such modules (dead_code 0). A `src/main.py` now
  imports it; the `>= 2000` dead_code and 30 KiB brief bounds are unchanged.
- No product-source change.

## 0.2.0 — 2026-11-21 (local; not tagged, not published)

Existing projects: `intake`, `slop`, `history`, `map` (one read-only manager call), at most four
questions, `plan` with content-bound approval, and standard-lane `task` / `plan --run` with
contract.lite, an isolated gate copy, L0 checks and a cross-family L1 evaluator. Known gaps: CI has
not run (no push), the review profile and L1 have not run with real tools, the default suite takes
about 75 s instead of 60 s. See the v0.2 closure review for open P2 items.

### T48b — the macOS gate sandbox cannot start processes outside itself

- L2 review T48 (P2-1, raised to P1 by a main-session test): the Seatbelt profile was
  `(allow default)` with only file-write and network denies, so gate-run project code
  could call `/usr/bin/open -a <App>` and LaunchServices started the app outside the
  sandbox (measured with TextEdit). Such an app (for example Terminal running a
  `.command` file) could write anywhere, including orcexe state, after the step ended.
- `isolation.profile` now adds `(deny lsopen)` and `(deny appleevent-send)`. The rest
  of the profile is unchanged (still `(allow default)`); git, Python, subprocesses and
  writes inside the checkout/step temp keep working.
- Tests: `tests/unit/test_isolation.py` checks that the generated profile contains both
  denies. `tests/slow/test_isolation.py` (opt-in: `--slow` / `ORCEXE_SLOW_TESTS=1`, because
  its control run launches an app outside any sandbox and registers it with LaunchServices)
  runs `/usr/bin/open` on a throwaway background-only probe bundle inside the real profile:
  the probe writes a marker outside the writable directories and exits, an unsandboxed
  control launch proves the host can start it, and the sandboxed open must fail with no
  marker. Skipped when `sandbox-exec` or LaunchServices launching is unavailable. Both
  tests fail without the denies.
- README: the "children and daemons inherit the policy" claim is replaced with what is
  enforced on macOS and Linux; a mach-lookup allow-list and `bwrap --unshare-all` are
  planned for v0.3.

### T48a — the L1 verdict is read only from output that cannot carry a second verdict

- L2 review T48 (P1): T47g let `mapping.parse_json_output` accept prose around one
  ```` ```json ```` fence and ignore the prose, and the L1 evaluator used the same
  parser. An evaluator that quoted a passing ```` ```json ```` rubric from the
  executor-controlled diff and wrote its real, failing verdict as bare JSON outside
  the fence had the quoted block read as the verdict, so the task could reach
  `accepted`.
- New `mapping.parse_strict_json_output`, used only by `task_l1.parse_rubric`. Valid
  output is a bare object (the whole stripped reply) or exactly one ```` ```json ````
  fence starting at column 0 and closed by a bare ```` ``` ````, where the text before
  it has no `{` or `}`, no line anywhere is fence-like besides the two fence lines
  (```` ``` ```` or `~~~` after any indentation or `>` blockquote markers), and only
  whitespace follows the closing fence. One plain sentence before the fence stays
  valid. Anything else is `l1_invalid`, so the task stays `gate_passed`; there is
  no retry, as for other invalid rubrics.
- Every rubric rule is unchanged: closed schema, byte-for-byte `d1..dN` IDs,
  `overall_pass == all(pass)`, reason caps and the same-model refusal.
- Manager output (`map`, incremental `map`, `plan`) keeps the T47g tolerance;
  `parse_json_output` is unchanged and its results still pass human approval.
- Tests: `tests/unit/test_task_l1.py` covers a quoted fence with a bare verdict
  before or after it, a verdict in a `~~~json` fence, indented and blockquoted
  quoted fences, an indented real fence, prose after the closing fence, braces in
  the prose, an extra fence line, the still-valid intro sentence + fence and bare
  object, the unchanged manager parser on the same input, and an end-to-end task
  whose evaluator smuggles a passing block beside a failing verdict and ends
  `gate_passed` / `l1_invalid`. Both new tests fail against the old parser.
- README: evaluator output is strict, manager output tolerant.

### T47 (landed by orcexe as plan task T1) — `state_dir_in_repository` is reported by `map` and `plan`

- `mapping.manager_run` already refused an `ORCEXE_STATE_DIR` that resolves inside the
  repository or its main worktree, before creating any state directory or calling the
  model. The reason was lost in `report_failure`, which only reports closed expected
  reasons, so `map` printed `not_seen: manager_unavailable` and `plan` printed
  `not_seen: manager_unavailable: failed`.
- Both commands now exit 2 with exactly `not_seen: state_dir_in_repository` on stderr,
  nothing on stdout, no manager model call and no published `plan.json`. The reason is
  in the tuple `mapping._map_command` passes to `report_failure` and in
  `planning.EXIT_REASONS` (USAGE); `plan` still derives its expected list from
  `EXIT_REASONS`, so there is no second list.
- README (map and plan reason tables, state-location row) and SKILL (plan table and
  shared table, as `map: ...`) gained the row; `tests/security/test_reason_docs.py`
  is unchanged. SKILL.md is 151 lines.
- New tests in `tests/unit/test_mapping.py` and `tests/unit/test_planning.py` cover a
  state dir under the repository, a nested one and the repository root itself, each
  asserting the exact exit code and stderr, an empty stdout, no model call and no
  state written inside the repository.
- Validation (gate run by the main session after landing): default runner passed 920
  tests, 0 failures.

### T47g — manager output tolerates prose around one JSON fence

- A real incremental `orcexe map` reply put one sentence and a blank line before a
  single ```` ```json ```` block holding a valid map; the strict parser rejected it as
  `manager_output_invalid` and the paid call was wasted.
- `mapping.parse_json_output` (shared by `map`, `plan` and the task L1 review) now
  accepts a bare JSON object, exactly one ```` ```json ```` fence alone, or exactly one
  such fence with prose before and/or after it. The prose is ignored: never parsed,
  never logged, never published.
- Still rejected (`detail: output invalid_json`): two or more fence lines of any
  language, a fence whose language is not exactly `json` (including a bare or
  longer fence), JSON in prose without a fence, an unclosed fence, anything after
  the object inside the fence, duplicate keys and JSON constants. The 256 KiB
  output cap counts the whole reply, prose included.
- The map brief (full and incremental) and the plan brief now say the reply must be
  the JSON object only, with no text before or after it.

### T47f — the default suite passes in a checkout without `.git`

- Gate checks run in a plain file checkout of the candidate tree, which has no
  `.git`. orcexe's own default suite errored there in
  `test_history.test_repository_child_cpu_and_python39_import`, which scans this
  repository with git.
- That CPU/import budget and the opt-in history wall budget moved to
  `tests/slow/test_history.py` (slow-only subclass, same assertions with
  `scaled()`). The opt-in repository wall-clock scans of `slop` and `intake` moved
  from the unit modules into `tests/slow/test_slop.py` and `tests/slow/test_intake.py`,
  so the default suite no longer collects any test that reads this repository's git.
- New `tests/unit/test_default_suite_no_git.py` runs no git: it discovers the default
  suite with child processes forbidden and statically rejects any collected test (or
  a same-class helper it calls) that runs a child with this repository's root as `cwd`,
  hands that root to a git-backed orcexe module, or joins it with `.git`. A self-check
  confirms it flags the scans kept in `tests/slow/`.
- README gate section: gate checkouts have no `.git`; project checks must not
  depend on it.

### T47e — approval refuses lanes this version cannot run; verbs inside identifiers are not actions

- `plan --approve` refuses a plan in which any task's final lane is not supported
  by this version (only `fast` and `standard` run): exit 2,
  `not_seen: lane_not_supported` plus one `lane_not_supported: T<n> <lane>` line
  per task on stderr. Nothing is written and the plan stays `proposed`, so
  `plan --run` refuses it as unapproved before any dispatch. The check runs after
  the existing tamper validation.
- `plan --goal` still publishes such a plan, but marks each unsupported task with
  `"lane_note": "lane_not_supported"` in the JSON, `risky (lane_not_supported)` in
  the `plan.md` lane cell, and a "Not runnable in this version" section. The note is
  bound by the content hash; a note on a supported lane or with another value is
  `plan_tampered`. Plans written before T47e (no note) are still refused.
- `lane_not_supported` is documented in the README and SKILL plan reason tables.
  `planning.SUPPORTED_LANES` is now shared with `task`'s lane check.
- Classifier: outward-action and destructive stems count only as plain prose words.
  A stem that is only part of an identifier — snake_case, camelCase / PascalCase,
  dotted names and calls (`git.commit_count()`, `commit()`), file-like tokens (a
  file extension such as `mailer.py`, or a leading `./`, `../`, `/` or `~/`), or a
  backticked name without spaces — is not that action, and a multiword stem cannot
  span an identifier. Prose (`commit the change`, `send an email`,
  `Değişikliği gönder`, `PUSHsuffix`, `_push_`) signals as before, and so do
  kebab-case and slash-joined words (`force-push`, `hard-delete`, `re-deploy`,
  `pre-commit`, `commit/push`); false positives such as `publish-date` are accepted.
  Backticked text containing spaces is read as a command line, so its plain words
  (`git push origin`) still signal.
- Risk words are not loosened: risk stems inside identifiers still signal, and
  camelCase parts now also match (`userAuth` → `risk_word:auth`,
  `APIKey` → `risk_word:api_key`).
- Tests: approve refusal, `--run` refusal without dispatch, JSON/`plan.md` marks,
  note binding and legacy plans; identifier forms, prose forms, multiword spans
  and risk stems inside identifiers.

### T47d — retries gated on the cumulative tree; throughput budgets leave the default suite

- Standard and fast task gates no longer fail a retry (an attempt after the first
  in the same worktree) on the executor's per-attempt `empty_diff` warning alone.
  Whether anything changed is decided on the cumulative tree against the task base
  (`diff_collect`), and scope, text, secrets, test floor, project checks and L0/L1
  run on that tree as for attempt 1. A first-attempt `empty_diff` still fails
  `run_status`, a never-changed tree still fails `diff_collect`, and a retry whose
  own edits break the tree still fails. `do` (single attempt) is unchanged.
- Feedback for the next attempt names a project-check failure plainly (check id and
  detail) and asks the executor to re-check the current files instead of rewriting.
- A timed-out project check reports its limit, e.g. `unittest: timeout (600s)`,
  within the redacted 300-character detail rule.
- Move throughput budgets that fail on a loaded machine to `tests/slow/`
  (`--slow` / `ORCEXE_SLOW_TESTS=1`): slop and intake repository-scan CPU budgets,
  100000-record ledger verification and the 100000-path classifier budget. The
  history wall budget now uses `scaled()`.
- Add gate_lite and task-level regressions for retry-after-timeout (standard and
  fast), empty first attempts, never-changed trees, retries that break the tree,
  the timeout detail and an invalid `retry` argument.

### T47c — literal-only constant assertions

- Parse JS/TS and Swift assertion arguments as literal expressions, retaining
  constant containers and rejecting calls (including masked local helpers),
  identifiers, member access, await and interpolated strings. Recognize numeric
  literal forms and keep each language's null/nil rules separate.
- Report Swift constant-only XCTest and Testing checks as `constant_assertions`.
  L0 continues to share slop's assertion detector without a separate decision.
- Add call-versus-literal regressions for Python, JavaScript, TypeScript and
  Swift, including nested containers, operators, matcher arguments and strings.
- Validation on Python 3.9.6: default runner (`-j 4`) passed 901 tests in 60
  modules (23 skips); focused assertion/slop/L0 and stdlib checks passed 68 tests
  (1 skip). Python 3.9 syntax and diff whitespace checks passed.

### T47b — assertion gate regressions

- Recognize Swift Testing `Issue.record` and XCTest `XCTUnwrap`; follow Swift
  local, method, extension and test-support helpers up to three calls, without
  borrowing checks from uncalled declarations, other suites or private support.
- Preserve mixed literal/dynamic imported assertions, including Deno versioned
  specifiers and assertion aliases. Keep constant-only slop findings and ensure
  product calls such as `Rules.check(2)` remain hollow in every supported language.
- Give L0 the same helper context as slop using bounded source snapshots from
  frozen base/candidate copies, captured before test execution. Reuse Python AST
  nodes so L0 can resolve local helpers as well as imported support.
- Add synthetic check/hollow pairs for TS/Deno, Python, Swift Testing and XCTest,
  plus scope, depth, cycle, argument and frozen-support regressions.
- Validation on Python 3.9.6: default runner (`-j 4`) passed 900 tests in 60
  modules (23 skips); focused assertion/slop/L0 tests passed 65 tests (1 skip).
  Stdlib-only checks and Python 3.9 syntax validation passed.

### T47a — assertion, dead-code and dependency precision

- Share slop's assertion detector with L0 assertion/hollow checks; recognize
  raise/throw-style checks, Deno std assertions, receiver-independent assertion
  prefixes, failure calls and XCTest/Swift Testing checks. Preserve constant-only
  and product-only slop signals and bounded test-helper resolution.
- Exclude same-file references and Python scripts from dead-export findings;
  retain advisory unused names in imported modules and packages.
- Recognize Deno specifiers/import mappings, repository-local Python modules
  (including sys.path directories), Python lock declarations and Node locks.
  Keep undeclared third-party imports visible without executing project code.
- Add synthetic positive/negative pairs and an L0 raise-style unittest regression.
- Validation on Python 3.9.6: default suite with four workers passed 894 tests
  (23 skips); extended L0 passed 8 tests. The first CPU-count worker run hit an
  unrelated doctor timeout-budget assertion; it passed with reduced concurrency.

### T46 — v0.2 security, documentation and suite speed (C23, C25)

- Inventory every subprocess-layer caller, including shared manager, evaluator,
  task/plan execution and project gate/L0/red-first paths. Observe real fake-child
  argv/environments with the v0.1 secret detector; check exact Codex trust writes,
  HOME files/directories, fresh project HOME cleanup and OS sandbox policies.
  Live sandbox probes deny outside/symlink writes and loopback connections when
  the host permits sandbox creation; unavailable hosts explicitly skip that probe.
- Derive command reason/code tables from executable constants and diagnostic
  branches. Check README and SKILL for every mapping, meaningful third columns,
  duplicate/stale rows and command-specific differences.
- Document the v0.2 approval/execution/manual-landing flow, standard checks and
  attempts, OS isolation limits, all write locations and manual cleanup, plus
  executor hooks/plugins and tool-owned user configuration. Consolidate SKILL
  reason rows to retain the existing 160-line limit.
- Move extended end-to-end/publication/race/retry/interpreter matrices under
  tests/slow; retain fast examples, validator matrices, atomic-publication checks
  and representative cross-adapter process contracts. Add --slow as an alias for
  ORCEXE_SLOW_TESTS=1; both modes print measured wall totals without a timing assert.
  Slow mode defaults to a 1800-second module budget for the existing full
  redaction stress pack; explicit --timeout continues to override it.
- Validation: default runner passed 882 tests in 59 modules in 59.70 seconds
  (0 failures/errors; 22 explicit skips), down from the 910-test baseline at
  115.58 seconds. Python 3.9.6 passed the full slow suite: 926 tests in 66
  modules in 613.90 seconds (0 failures/errors; 15 explicit skips), plus the
  focused security/runner/L0 checks. Live OS confinement is unavailable on this
  host; policy construction and unavailable-backend behavior remain tested.
- A focused runner test exposed macOS /var versus /private/var temp-path aliases:
  normalize the discovery start path along with its top-level path. Add a real
  worker proof that --slow propagates. No product-source change was needed.

### T45 — bounded fast-lane vocabulary (K6)

- Add four bilingual documentation intents: update a document, add an example,
  fix a source docstring and clarify a document; accept bare or backtick targets.
- Keep full-task matching, target classes, risk precedence and length limits;
  reject Unicode disguises, shell-bearing targets and unresolved directory paths.
  Deny licence/manifests, nested CI and i18n targets, including resolved basenames.
- Document every fast form in README/SKILL and add bilingual positive and
  adversarial regressions without changing existing classifier tests.

### T44b — regenerate current-state with all human blocks

- Always refresh current-state.md, preserving every human block in order after
  the generated summary, including user-added blocks and malformed tails.
- Save outside-block edits once in private current-state.previous.md and report
  the backup; avoid proposals and duplicated generated summaries.

### T44 — incremental maps and preserved human context

- Update maps incrementally from ancestor intake_sha using changed-file-only
  manager copies; retain omitted entries and recheck evidence at HEAD. Add
  --full, a 40% changed-path fallback, update metadata and model-free no-change runs.
- Record private publication hashes/baselines. Preserve human JSON entries with
  source: human and persistent deletions; expose evidence-backed restorations as
  proposals. Keep edited text with adjacent proposals and human_edit_kept paths.
- Maintain current-state.md from CLI map/plan/ledger/ref data across map, plan,
  task and plan runs, preserving its delimited human block verbatim.

### T43 — stacked, resumable plan execution

- Add `orcexe plan --run PLAN_ID [--resume]`: sequential deterministic dependency
  ordering through the existing task gates, transitive failure blocking, risky
  lane refusal and final-plan no-op re-runs.
- Stack accepted frozen commits under one private plan ref. Single tasks and
  plan runs start at its tip; main HEAD, index and working files stay unchanged.
- Close interrupted attempts, remove their worktrees and resume within the same
  contract/hash and remaining attempt budget; refuse implicit interrupted re-runs.
- Add read-only, ledger-derived `orcexe status --json` and private atomic
  `.orcexe/status.md`, with task outcomes and a manual integration land command.

### T42d — gate isolation and stronger L0

- Run all task/do project checks and L0 base/red-first steps with Seatbelt on
  macOS or bwrap on Linux; writable checkout/temp only, outbound network denied.
  Record per-attempt isolation; unavailable backends cap tasks at gate_passed and
  keep do unverified with isolation_unavailable.
- Snapshot CLI control state around each code step; terminal state_tampered and
  persistent dependency refusal prevent ledger/control tampering from approving work.
- Compare static test definitions and final runtime counts; reject runtime skips,
  hollow tests and changed tests without assertions. Match red-first checkout path
  shape to gate paths and apply risk-path checks to fast tasks too.
- Document known-weak red evidence from imports of new symbols; add regressions
  for isolation policy, tamper, no-sandbox execution, count spoofing and L0 forms.

### T34–T42c — intake, slop, history, map, questions, plan, standard task

- Share repository risk areas across planning, gate checks and escalation; judge any-depth plan risks by concrete paths, reject standard risk touches immediately with `risk_path_touched`, and reject non-documentation standard/risky plans without an L0 test path (`no_test_path`).
- Complete standard `task` acceptance with L0 test-count, added skip, test-change and red-first checks; independent read-only L1 evaluation with strict ordered rubric IDs, fail feedback, shared retry budget and content-free ledger verdicts.
- Harden plan safety with cleaned-text validation, NFC/casefold path comparisons,
  directory glob coverage, shared local risk-map signals, global question decisions,
  content-bound approval with repeated safety checks, and main-worktree state.
  Reject tampered forms and corrupt approval state; show full task scope in Markdown.
- Defer goal-scoped risk and do-not-touch confirmations to the plan approval, show lane-raising areas in Markdown, and require path/module relevance for unknown questions. Accept protective globs naming state and excluded paths without raising unrelated tasks.
- Share bounded glob validation across maps, questions and plans, conservatively raise lanes for unusable risk globs, distinguish computation failures from invalid manager output and writes, and exclude common tracked-path terms from relevance. Replay the unmodified second real plan in offline regression coverage.
- Filter questions by goal relevance and share a four-answer budget across forms using goal hashes. Declare map/plan caps, retain optional task notes in JSON/Markdown, and report content-free validation locations and rules.
- Add `orcexe map`: one read-only manager call over HEAD evidence, strict manager JSON validation, evidence-checked project maps, hotspot coverage, private capped history/direction reports, and offline fake-CLI proofs. Project commands remain unverified.
- Restrict map managers to Claude with command/web tools denied; isolate briefs in external state, omit nested agent instructions, normalize repository roots, publish all four outputs through one generation swap, and harden rendering, hotspot globs, cancellation and HEAD checks.
- Give map and plan a dedicated Claude manager profile with measured restricted/safe-mode isolation and a Read/Glob/Grep allow-list; normalize customization names with NFC/casefold, restore identity redaction, bound protective glob processing, and retain snapshots across rollback failures. Clean stale publication artifacts, suppress invisible prose/autolinks, handle SIGHUP/SIGQUIT, and classify discovery write failures correctly.
- Deny Claude review Bash/WebFetch/WebSearch explicitly (plan mode alone permits Bash); pin all three tools' review argv and document their differing limits.
- Give map managers named 900-second timeout/300-second stall budgets, distinguish unavailable reasons without changing exit 2, require file-reading tools in the brief, and compact evidence into counts plus at most three deduplicated examples per section.
- Harden gate checkouts using raw committed blobs; refuse committed symlinks escaping the copy, including through the linked `node_modules`.
- Add model-free `history` with private U2/U3 evidence, HEAD-relative history windows, committed direction documents, identity redaction and opt-in bounded GitHub issue discovery.
- Resolve asserting test helpers to depth three and mark conditional skips advisory in `slop`, retaining product-only fake tests and unconditional skip signals.
- Add model-free `slop` scanning of committed Python/JS/TS and selected Swift signals, private atomic JSON/Markdown reports, advisory unused exports, dependency checks and prioritized fix lanes; reuse intake's bounded reader and safety controls.
- Add internal model-free `intake` discovery from bounded committed Git blobs, private U1 JSON, opaque instruction fingerprints, candidate commands and declaration-only security evidence; never fetch secret paths or execute project code.

## 0.1.0 — 2026-11-18 (local tag; not published)

Walking skeleton: `orcexe do` runs one fast-lane task end to end with Codex, Claude Code or Cursor in an isolated worktree, gates it with the project's own checks and leaves landing to the human. Known gaps: CI has not run yet (no push), the review profile was not run against the real tools, native Windows is unverified.

- Report doctor's configured per-probe timeout budget even when the shared total deadline leaves less execution time.
- Give the manual-run integration fake's Cursor model preflight a scaled setup budget; retain early-exit stdout/stderr diagnostics before signal handshakes.
- Freeze gate commands and classifier config before executor dispatch; detect main `.orcexe` config byte changes.
- Restrict Claude/Cursor error markers to final error results and non-JSON diagnostics; successful quoted markers remain successful.
- Expire Claude quota observations after six hours and honour reset times; show cache age in doctor.
- Grant Claude/Cursor only `run_dir/brief/` and write private run evidence without following file or directory symlinks. Their argv pins deliberately change; Codex argv is unchanged.
- Use unittest `-s tests -t .` when `tests/__init__.py` exists and `-s tests` when absent; reject zero-test evidence, log both Python interpreters, honour `ORCEXE_STATE_DIR` for gate checkouts, and return exit 2 for manual-run `pipes_held` warnings.
- Document tool selection, project commands/hooks, Cursor trust, write locations, and clearing the quota cache.

- Add an offline security test package covering source-wide network imports, child credential isolation, forbidden argv, private reports and HOME writes; preserve prompt operands as free text; fixed: no socket import; identity lookup uses a minimal env.
- Add SHA-pinned GitHub Actions CI across three operating systems and Python 3.9/3.13, experimental Windows reporting, a targeted weekly/manual slow suite, workflow safety tests, and CI cost guidance.
- Add the Claude Code plugin surface: the `orcexe` skill (doctor first, verdict-driven `do` reporting, human-only landing, exit codes) and the `/orcexe` command, plus `scripts/check_marketplace.py` validating marketplace/plugin manifests, version agreement, skill front matter and size, and command descriptions.
- Add `orcexe run` for one manual executor in a chosen Git worktree, with observed-status exits, private atomic receipts, signal cancellation, Codex trust recording, and offline integration coverage for all three tools.
- Give doctor probes 10-second per-operation/session budgets within a shared 60-second budget, keep tool probes concurrent, and report timeouts with recovery commands. Cover slow Cursor model lists and string/dict model ids; treat host-provided `ANTHROPIC_BASE_URL` as informational inside Claude Code while retaining credential warnings.
- Add model-free `doctor` and single-operation `probe`, private validated environment reports with a 60-second cache, actionable readiness checks, and presence-only Claude Code host detection with H1–H7 evidence.
- Record new Codex worktree trust headers in a private ledger; add `trust list` and preview-first `trust clean` with confirmation, private backups, edited/live table protection, and byte-preserving cleanup.
- Restore Cursor's five default-model config keys after every execution outcome (C1), preserving unrelated config changes, key order and file mode; leave `statsig-cache.json` alone as an analytics cache. Report sandboxed shell usage as `shell_used:N`; reserve `shell_blocked` for an explicit refusal (measured 2026-11-17).
- Add the measured offline Cursor `agent` fake, subscription-safe Cursor adapter, effort slug selection, and executor routing fallback.
- Add the measured Claude Code fake and `claude-code` adapter with subscription-safe probes, observed quota caching, Claude routing priors, and routed-tool dispatch.
- Put Claude's prompt immediately after `-p` so variadic options cannot swallow it; model variadic directory/tool options and missing-input errors in the offline fake. Mark the argv ordering as inferred from `--help`, pending a real-run check.
- Parse Claude auth status as one bounded JSON document, matching the pretty-printed output measured on 2.1.310; discard raw account data after allow-list extraction. Default the offline fake to that format and cover compact JSON compatibility and oversized/malformed output. Executor stream-json remains one object per line.

- Add an isolated parallel stdlib test runner with duration-based scheduling, faster Git fixtures, deterministic default fuzz samples, opt-in full timing checks, and parallel stress runs.
- Observe main HEAD (symbolic and object), refs outside the current task branch, worktree status excluding `.orcexe/`, and T25 Git controls around the executor. Any change fails `main_repository_changed` before the gate.
- Resolve Git identity after classification and routing, before any worktree or executor (including dry runs), using read-only config queries in the normal user environment with hooks disabled. Git's per-field precedence applies: `GIT_AUTHOR_NAME/EMAIL` and `GIT_COMMITTER_NAME/EMAIL`, then `author.*`/`committer.*`, then `user.*` from repository/global/system config. Missing name/email blocks with `git_identity_missing` and FAIL before dispatch. Pass the captured identity explicitly to hardened `commit-tree`; no identity is invented.
- Freeze accepted edits in `refs/orcexe/accepted/<task>` using a private index, the base target mode and gate-verified `diff.target_blob`/`diff.target_path`; construct and verify a single-parent commit without reading executor worktree bytes. Landing advice merges or cherry-picks that ref. Target paths in receipts retain existing secret redaction.
- Treat `pipes_held` as unverified; persist `status.md` before the terminal ledger state. Reject invalid model constraints with USAGE and refuse a symlink at main `.orcexe` before creating a worktree.
- Cancel the adapter on POSIX SIGINT/SIGTERM/SIGHUP, report `cancelled` with `signal_<name>`, release the lock and restore handlers. Conventional `128+signum` is exposed as `signal_exit_code` and mapped to CLI FAIL (1). Locks record pid/created_utc/task_id/host; dead local pids are reclaimed immediately and verified ledgers receive `failed: abandoned`. Remote-host, malformed and live-pid locks remain blocked. SIGKILL cannot clean up an already-running executor tree; recovery reports its retained worktree for inspection. PID reuse can still conservatively block reclamation.


- Add `orcexe do` for one isolated fast-lane attempt with model-free routing, exclusive locking, gate-authoritative reports, redacted ledger evidence, write-free dry runs, and human-only landing instructions.

- Add prior-based stage routing with a validated experience pack, model-free access snapshots, user constraints, quota filtering, effort adjustment, and explicit decision reasons.
- Add a locked, append-only project ledger with redacted allow-list event schemas, required fields, bounded integers and closed vocabularies, SHA-256 chain verification, and measured/not-seen task KPIs including median time to first dispatch; reject invalid known fields before writing.

- Disable Git replacement objects and grafts in every gate Git call; fingerprint loose/packed replacement refs, grafts, shallow boundaries, alternates and worktree-local controls. Before running commands, verify the complete private checkout against base-tree blob OIDs in one hash batch, with the target checked against its verified bytes; incomplete or mismatched checkouts remain unverified.
- Treat PHP and Ruby source edits as `code_change_unverifiable` (escalated) in the v0.1 fast lane. Preserve `#` as code in all supported C-like languages, including C/C++ preprocessor directives and Rust outer/inner attributes.
- Require pre-executor Git-control fingerprints and branch binding; inspect every commit, added bytes, whole-file secrets and comment/docstring-only source edits, then run project checks in a private base checkout with verified target bytes and an isolated home (main `node_modules` is user-owned, linked with read-only intent). Always replace the gate receipt on failures.
- Gate the union of base-to-HEAD, base-to-index (read-only), and raw worktree diffs. Each distinct file version is checked for scope, whitespace, secrets and test-floor regressions; identical diffs count once, while differing versions share the summed line budget. Versions of the allowed target may differ; any change outside it fails scope, even when restored on disk.
- Reject all untracked files, including ignored and self-ignoring files, with `scope: untracked_ignored:<count>`. The closed cache exemption permits only regular `*.pyc` files inside `__pycache__/` directories and regular files inside `.pytest_cache/`, `.mypy_cache/`, `.ruff_cache/`; `.gitignore`/`.gitattributes`, symlinks and special files are never exempt. No exemptions for `node_modules/`, `.env*`, `build/`, `dist/` or `*.egg-info`.
- Validate the worktree's root gitfile against trusted main-repository registration before using it; reject replacement directories, changed pointers and symlinks, and refuse nested `.git` entries (including embedded repositories and submodules) without following them.

- Add a deterministic fast-lane gate with raw temporary-index diffs, scope and test-floor checks, base-defined project commands, redacted evidence, and fail-closed acceptance (VARSAYIM: 50 changed lines; project commands have no network sandbox).
- Add per-task managed Git worktrees with persistent ownership receipts, main-worktree write guards, and bundle/patch archives verified before removal.
- Exclude third-party and generated trees from fast classification, sample classifier combinations by default (full set with `ORCEXE_SLOW_TESTS=1`), and make post-stop pipe draining independent of leader-reaping timing while keeping denied stops bounded.
- Require complete English/Turkish editing patterns for the fast lane, with allowed document/source targets, closed vocabulary and suffixes (up to three), and limits of 200 characters and 25 tokens. Unknown text and excluded targets select standard; existing risk rules retain precedence and project risk maps remain add-only.
- Harden lane classification with NFKC/Unicode format-character normalization, mixed-script detection, destructive and outward-action signals, sensitive-data stems, and broader scope detection. Conservative prefix matching also flags benign uses such as `postgres`, `tagline`, and `apply formatting`.
- Add a deterministic, conservative lane classifier with additive project risk maps and NUL-safe tracked-file discovery.
- Match Codex 0.163.0 shapes measured 2026-11-16: stderr login status, app-server envelopes without jsonrpc, interleaved notifications, full synthetic rate-limit/model fixtures, and model reasoning efforts. Count ignored protocol noise and keep account identifiers out of probe results.
- Stabilize per-detector growth measurements with interleaved minimum timings and bounded re-measurement near the unchanged 2.5 threshold.
- Add the Codex adapter with bounded app-server probes, confirmed quota/auth classification, and hashed trust warnings; v0.1 supports ChatGPT login only and never forwards API keys.
- Add the shared adapter lifecycle, validated run results, Git snapshots, and adapter contract/manifest parity suites.
- Make timing tests load-tolerant with readiness handshakes, scaled bounds, CPU-time performance checks, and a repeatable CPU stress harness.
- Add the repository skeleton, CLI launchers, and version command.
- Add a bounded streaming process runner with explicit environments, timeouts, cancellation, and process tree cleanup.
- Add a strict, bounded YAML subset parser with source-position errors and deterministic configuration types.
- Add validated immutable capability manifests, evidence coverage, safe argv construction, and the Codex manifest.
- Add an evidence-labelled fake Codex CLI with exec streams, login, app-server fixtures, and quota-free runner tests.
