## Unreleased

### T68 — lane classifier v3: T58 L2 P2-1, P2-2, P2-4 and decision 028

- `classify.CLASSIFIER_VERSION = "lane.v3"`. Plans recorded with `lane.v2` are `plan_stale`
  (`detail: classifier_changed`) at approval, `--open`, `plan --run` and `task`
  (`test_partial_approval.test_lane_v2_plans_are_stale_under_lane_v3`).
- P2-1 (done/notes, and the goal): a bare outward verb (`push publish upload deploy send email
  mail merge rebase commit post tag`) counts anywhere in a sentence (A8) when an object follows
  it (a determiner/pronoun, an identifier or command, or `to/into/onto`) and it has a direction
  (`to/into/onto` within 6 words) or an A8 noun within 3 words (nouns gain `build crate
  payload`). It does not count after a determiner (`the push to origin`), after a negation
  (`do not push`, `never push`, `not to push`), inside a kebab/slash word (`post-merge`) or as a
  compound modifier (`commit message`). Passive state sentences (`is pushed`, `are then
  committed`, `is sent`) count unless negated back to the clause start (`nothing is pushed`,
  `No plan.json or plan.md is published`); `merge`, `post`, `tag` are excluded there. `post`
  and `tag` join the inflected A8 forms (`POSTs`, `tagged`). A word after `:` or `,` starts a
  clause for A4 (with an object or the sentence end) and for bare verbs (`TODO: push …`,
  `After that, push the branch.`). A3 pairs add `cargo publish`, `compose push`. Turkish:
  `<push|commit|deploy|merge|rebase|publish|upload|tag> + et/edin/edip/edilir/yap/yapın/atın…`
  (bare `at` only at the sentence end or before `ve`/`sonra`), `yayına/canlıya al…`, and
  suffix + continuation forms of the A4-TR stems (`yükleyip`, `gönderin`, `yayınlasın`) count
  whatever follows.
- P2-2: A5 (run verb + outward part) scans identifier and command parts by prefix as before; a
  prose word counts only in its full form (`deploy`, `merging`, `published`) and only as the run
  verb's object (at most three determiners between: `Execute the deploy step`). `source` is a run
  verb only before an identifier or command. A mid-sentence "lead + verb" (A4) needs an object
  or the sentence end (`pre and post conditions`, `Branch and tag names` stay quiet). The
  passive aorist of the ambiguous Turkish stems `yükle birleştir paylaş` (`yüklenir`,
  `birleştirilir`, `paylaşılır`) describes and is not A4-TR.
- r2 (gate findings): exemptions only stop new v3 signals. Kebab/slash prose parts keep lane.v2's
  A5 prefix match (`Trigger the mirror-push job`); a prose run object may be a noun phrase of up
  to three words that contains no conjunction, preposition or clause break. The Turkish passive-aorist
  exemption applies only without a destination: no word before the verb ending in -a/-e
  (dative/locative such as `kanalında`, `sunucuya`; function words and `-sa/-se` conditionals
  excepted), no apostrophe suffix ending in -a/-e (`PyPI'ye`, `` `main`'e ``), and no A8 noun.
  `Özet ekip kanalında paylaşılır.` and `Paket PyPI'ye yüklenir.` stay risky; `Koşul sağlanırsa
  etiketler birleştirilir.` does not. A sentence-start A4 verb followed by a bare noun phrase that
  is the subject of a finite verb (`is are was were stay remain become has have must should will
  can may`) within six tokens, with no direction, relative pronoun or clause break before it, is
  descriptive (`Tag names in the parser are case-insensitive.`); `Push changes to origin.`, `Tag
  the release.`, `Push branches that are ready.` stay risky.
- r3: the descriptive start-verb rule is narrowed to its main-verb form: one or two bare nouns,
  then optionally one prepositional phrase (not after/before/to; at most three plain words) or
  one participle, then directly the finite verb. Any subordinator or clause joiner (when, after,
  before, once, until, if, so, because, while, as, unless, then, since, though…), relative
  pronoun or clause break before the finite verb keeps the imperative: `Publish builds when they
  are ready.`, `Merge branches after CI has passed.` stay risky. Tests: 12 imperative +
  subordinate clause sentences (push/publish/upload/merge/tag/deploy/send/commit) risky in done
  and notes (and risky under the frozen v2), and the quiet twins `Tag names in the parser are
  case-insensitive.`, `Commit messages are validated.`, `Deploy scripts must be idempotent.`,
  `Post titles stay under 80 characters.`. The last two were lane.v2 A4 catches, so they join the
  v2-subset allowlist (14 entries).
- Structural v2 subset: `tests/fixtures/classify/lane_v2.py` is the lane.v2 classifier frozen
  verbatim (0b7bcb6). `test_classify_v3.V2SubsetTests` runs it and lane.v3 over every
  `test_classify_v2`/`test_classify_v3` sentence (including today's v1 test texts) as goal,
  done and notes with three file sets, and asserts `lane_v3 >= lane_v2`, except an explicit
  allowlist of P2-2/r2/r3 false alarms (12 after r2, 14 after r3), each with a reason. The goal field never drops, and every
  allowlist entry must really drop, so stale entries fail.
- P2-4: one line break is a space; only a blank line or a bulleted/numbered line starts a new
  sentence. `e.g.`, `i.e.`, `vs.`, `etc.` and one letter plus a dot (`J.`) never end a sentence.
- Decision 028: a present but unusable `.orcexe/risk-map.yaml` (corrupt, empty, binary, wrong
  type/key/version, any symlink — including one to a valid file —, unreadable) makes both
  `classify_repo` and `task_lane_repo` return at least `risky` with `risk_map_invalid`, also for
  a harmless text (README typo); `risk_areas` records that area as `risky`. A missing map changes
  nothing; the `git_unavailable` floor stays `standard`. Tests that asserted `standard` for a
  broken map (`test_classify`, `test_classify_v2.RiskMapFloorTests`, `test_risk_areas`) now
  assert `risky`.
- Measurement (16 unique real plan tasks: 12 from the T47 reports, 2 from the local
  `.orcexe` plan state, 1 T62 plan, the T40c fixture; tracked files = `git ls-files`): risky
  v2 = 6, v3 = 4. The two dropped are the A5 false alarms of P2-2 (`plan3` T2 `published`,
  `plan4` T1 `publishes`); no task became newly risky.
- Tests: `test_classify_v3` (P2-1 catches/twins incl. Turkish, P2-2 twins and kept catches,
  P2-4 wrapped lines/abbreviations/sentence boundaries, decision 028 floors for both entry
  points, no-map/valid-map/git_unavailable unchanged, plan lane and risk area, goal never below
  `classify(goal)`); version assertions in `test_classify_v2` and `test_planning`.
- r4 (T68 L2, P1-1..P1-5, P2-2): architecture fix. A done/notes/goal field's lane.v3 signals
  are now `(lane.v2 signals − named exemptions) ∪ lane.v3 additions`, so the r1–r3 in-place
  narrowings no longer lower anything lane.v2 caught (`Fix lint and push again.`, `Run the
  deployment.`, `Sonra deploy işini tetikle.`, `Update version\nCommit\nPush\nDeploy`, `Rapor Slack
  ile paylaşılır.`, `Push branch is done.`, `Update docs, tests, etc. Deploy the site.` are risky).
  - `_v2_field_signals` computes lane.v2's field rules bit for bit (lane.v2's line-by-line
    sentences and its constants frozen as `_V2_*`); `V2BaseTests` compares its signals and
    sentences with the frozen fixture over the corpus and the grid, and the shared constants
    one by one.
  - `_v3_additions` (the r1–r3 rules on joined-line sentences) only adds. Its narrowing only
    keeps it from re-adding a false alarm.
  - The exemption pass is separate, done/notes only, per lane.v2 sentence and closed:
    `exempt:descriptive_subject` (verb + one head noun of `names messages notifications
    conflicts requests hooks keys permissions titles scripts limits fields` + optional one
    prepositional phrase + `is/are/stay(s)/remain(s)`; a modal, `has/have` or
    `done/complete/happen` anywhere breaks it), `exempt:prose_pre_post`,
    `exempt:prose_model_call`, `exempt:prose_source_of_truth`, `exempt:prose_starts_with`,
    `exempt:prose_starts_final` (run context that is only these noun/intransitive uses; A5 prose
    words only), `exempt:prose_published_outputs`, `exempt:prose_publishes_no`, and
    `exempt:tr_conditional_description` (passive aorist of `yükle birleştir paylaş` with a
    `-sa/-se` clause and `etiketler/ayarlar`, no `ile/üzerinden`, no destination). Each dropped
    signal is reported as `<field>:exempted` (`exempt:descriptive_subject A4=commit`).
  - Now risky again, as in lane.v2: `Deploy scripts must be idempotent.`, `Branch, tag and commit
    lists stay sorted.`, `Etiketler birleştirilir.`, `Run the tests and the published docs stay
    unchanged.`, `Run the publisher tests and note the pushback.`.
  - P2-4 additions: an abbreviation or one letter + dot before a capitalized word ends a
    sentence; bullets add `– — · >` and one-letter items (`a.`, `a)`).
  - P2-1 additions: `push'la`/`merge'le`, `-meyi/-mayı unutma`, `mail/email at`, `yayımla`,
    `to + bare verb` at the sentence end or before `it/them` (`Don't forget to push.`).
  - Tests: the r2/r3 allowlist is gone. `V2SubsetTests` (corpus, three file sets) and
    `TemplateGridTests` (1,818 generated sentences: P1-1 3×7×13, P1-2 6×6, line lists and
    bullets, abbreviations, Turkish passive with `ile/üzerinden`, work-step labels, descriptive
    heads, every review sentence) assert that every lane.v2 risky signal lane.v3 lacks has an
    `exempted` signal, that a lane falls only then, that exemptions fire only on the
    sentences tagged with that exemption's name, and that the goal is never exempted.
    `ExemptionTests` covers every exemption name with a positive sentence and near misses.
  - Measurement (same 16 real tasks): risky v2 = 6, v3 = 4. The two drops are
    `exempt:prose_published_outputs` (`plan3` T2) and `exempt:prose_publishes_no` (`plan4` T1).
- r5 (second L2 on r4): exemptions narrowed to their names.
  - Each run-context exemption drops only its own A5 words: `prose_model_call` → `published
    publishes`, `prose_source_of_truth` → `committed`, `prose_starts_with` → `tagline postal`,
    `prose_starts_final` → `truncated`. `After the model call, publish docs.`, `Once merged, the
    deploy starts.`, `Keep main as the source of truth for deployments.` stay risky.
  - `prose_publishes_no` needs an identifier or the sentence end after `no`;
    `prose_published_outputs` needs one to three identifiers or `.orcexe` between the words.
  - `tr_conditional_description`: only `birleştir + etiketler` and `yükle + ayarlar` (`paylaş`
    never), the object directly before the verb, and a Turkish conditional suffix (`-rsa/-rse`,
    `varsa/yoksa`) instead of any word ending in `sa/se`.
  - `V2BaseTests` adds a seeded differential over 300 messy generated notes (unicode, CRLF,
    fences, backticks, calls, identifiers, bullets, notes over 2,000 characters). Shared helpers
    used by the v2 base carry a "keep V2BaseTests green" comment. `_v3_additions` was not trimmed.
  - Measurement unchanged: risky v2 = 6, v3 = 4 on the 16 real tasks.
- r6 (rule R8): `test_classify_v3`'s full template grid, the 300-text random differential and
  the three-file-set loops run only with `ORCEXE_SLOW_TESTS=1`. The default run keeps every
  10th grid cell (every family) plus every review, exemption and near-miss sentence, and the
  first 40 random texts of the same seed (about 11s on Python 3.9, 44s in slow mode).

### T64b — slop precision on real repositories (C18 / K26)

- Assertion helpers resolve transitively in Swift too: a test may call a helper from a module it
  imports (a directory named like the import, e.g. `Sources/FooTestHelpers`) when that file itself
  imports XCTest/Testing, including `extension XCTest` members and generic `func Name<T>(...)`
  declarations. Every overload must check (a forwarding overload is judged by its sibling); a
  same-named API of an imported product module is a known product call and supplies no check.
  `#available` / `#unavailable` guards are masked, never checks.
- The shared check vocabulary (`_check_call`, also used by L0) accepts any capitalisation of the
  `assert` / `expect` / `verify` prefixes (`AssertHelp`, `VerifyX`); resolution still wins over the
  name, so a resolved helper that does not assert stays a fake test.
- Python tests are runner-collected names only: `test*` functions in `test*.py` / `*_test.py`
  files (the names L0 counts) or `TestCase` methods. Product functions named `test_*`
  (`task_l0.test_count`), functions nested in a function, and `@*.command` / `@*.group` /
  `@*.fixture` callbacks are not tests. A nested callback's assertions count once the enclosing
  test references it (`runner.invoke(cli)`) or registers it through a referenced callback
  (`@cli.command()`); a never-referenced nested function still supplies nothing. An unresolved
  `self.check_*` / `self.ensure_*` (mixin) counts as a check.
- Stubs: methods decorated `@abstractmethod` or documented as a subclass duty ("must be
  implemented by subclasses", "override") are not stubs; a defensive `else: raise
  NotImplementedError(case)` in test code is not product code. An undecorated, undocumented ABC
  method still is (the existing seeded case stays).
- Imports under `if TYPE_CHECKING:` / `if t.TYPE_CHECKING:` are not dependencies.
- Python runtime skips (`self.skipTest` / `pytest.skip`, not `xfail`) follow the marker rule: inside a
  non-constant `if` body/`else` (`if isolation.backend() == 'none':`) or an `except` handler, up to
  the nearest function boundary, they are `conditional_skip` (advisory). `if True:` and unconditional
  skips stay scored `runtime_skip`. JS/Swift unchanged.
- `missing_checks`: a committed `.swift-format` (or `.swiftlint.yml`) plus a committed script/CI
  file running it (`swift format lint`, `swiftlint`) is a lint check; only small script/CI blobs
  are read, through intake's bounded reader.
- Measured scored (non-advisory) findings, before → after: swift-argument-parser 211 → 6
  (fake_test 206 → 2, missing_checks 1 → 0, skipped_test 4 kept), click 79 → 52 (fake_test 27 → 4,
  stub 3 → 0, dependency 1 → 0; 47 suppressions and 1 xfail kept), orcexe 30 → 9 (fake_test 8 → 3,
  skipped_test 16 → 0 after r2; 6 `noqa` suppressions kept).

### T63c — the two remaining CI-only failures (C15)

- `is_repo`: an invalid `.git` gitfile is not a usable repository. Exit 128 with `gitfile does not
  point to a valid repository` (upstream git: Homebrew, Ubuntu), `invalid gitfile format` or
  `not a valid repository` now returns False, like Apple git's `not a git repository: (null)`.
  `dubious ownership` / `safe.directory` still win and raise their own error, even when the same
  stderr also names an invalid gitfile; a non-128 exit with these words still raises. The test
  fixture `non_repository_root` (a `.git` file pointing nowhere) therefore behaves the same under
  every git; this is what `test_codex_adapter.test_trust_header_hash_only_for_write_in_git` (via
  `CodexAdapter`'s `gitutil.is_repo(req.workdir)`) and the six non-git `test_run_fake` cases (via
  `run.py`'s `gitutil.is_repo(workdir)`) went through. `FakeGitStderrTests` gains these exact
  messages. Upstream git was not available locally: GÖRÜLMEDİ beyond the fake stderr (local git is
  Apple Git-157, `not a git repository: (null)`); the next CI run shows it.
- `tests/unit/test_risky_lane.py`: `RiskyFixture` and `RiskyPlanTests` simulate isolation evidence
  (`isolation.evidence` → `seatbelt`, `accepted_state = 'accepted'`), as `test_planrun` already
  does. Real gates and fake CLIs still run; only the evidence is simulated, and no assertion
  changed. On a host without a usable sandbox (ubuntu-22.04 + 3.9 in CI) every passing risky
  attempt otherwise ended `gate_passed` / `isolation_unavailable` instead of the auditor/L1 reason
  under test. A local run with the probe forced to `none` failed 9 risky-lane tests before (the 3
  seen in CI plus 6 more) and 0 after.

### T63b — diagnosable CI failures, CI-only breaks fixed (C15)

- `gitutil.GitError` carries `reason`, the git `returncode` and a short (≤240 chars), single-line,
  redacted `stderr_tail` with the repository path shown as `<path>` and the home directory as `~`;
  its text is `reason (exit N): tail`. `orcexe run` now prints `GitError: <that text>` instead of a
  bare `GitError`. The git runner keeps a 4 KiB stderr tail; git children keep `LC_ALL=C`/`LANG=C`
  and inherit no `LANGUAGE`.
- `is_repo` returns False only for exit 128 with git's discovery failure: `not a git repository`
  anywhere in stderr (case and line wrapping ignored; plain, ceiling directories, a gitfile naming a
  missing directory), `Stopping at filesystem boundary` / `GIT_DISCOVERY_ACROSS_FILESYSTEM`, or —
  only when the tail filled the 4 KiB cap — the discovery suffix (`git repository`, `(or any of the
  parent directories)`, `(or any parent up to mount point`). Warnings before the fatal line (e.g. an
  unreadable home config dir) do not matter. `dubious ownership` / `safe.directory` raise
  `GitError("Repository ownership is not trusted (safe.directory)")`, never "not a repository";
  anything else raises `Cannot determine whether path is a repository (exit N): tail`.
- Isolation probe: the backend check now creates a sandbox of the same shape `run` uses (bwrap:
  `--die-with-parent`, read-only root, two binds, `--dev /dev`, `--unshare-net`, `--chdir`; seatbelt:
  the real profile). A refusal (user namespace denied, e.g. Ubuntu 24.04 AppArmor; mount refused;
  timeout) means backend `none` → `isolation_unavailable`, and `isolation.unavailable_reason()` names
  the cause (exit code and bwrap's stderr). At run time a bwrap setup failure (`exit 1`, stderr
  starting `bwrap:`) records `none` for the session, so it is never counted as sandboxed. The
  security spawn inventory names `isolation._probe` instead of `_available_backend`.
- `test_real_sandbox_denies_outside_writes_including_children_and_symlinks` skips with
  `OS sandbox unavailable: <reason>` from the probe (measured, not per platform), and accepts
  `EROFS` as well as `EPERM`/`EACCES`: under bwrap a write to the read-only root bind fails with
  `EROFS`, which the earlier `except PermissionError` let escape as exit 1. Whether CI's Ubuntu
  host also denies the namespace: GÖRÜLMEDİ (not measured here; the next CI run's skip reason or
  pass shows it).
- CI: Python 3.9 runs on `ubuntu-22.04` (setup-python toolcache) and on `macos-latest` with the
  image's own `/usr/bin/python3` (Command Line Tools 3.9.6, setup-python skipped); 3.13 stays on
  `ubuntu-latest` / `macos-latest`; Windows stays experimental for both. A first step asserts the
  interpreter's major.minor matches the matrix. The slow workflow runs 3.9 on `ubuntu-22.04`.
  Actions stay pinned by SHA. The image contents are taken from their documentation, not checked
  online.
- Tests: `tests/unit/test_gitutil.py` `FakeGitStderrTests` (every variant above with fake stderr,
  cut tail, ownership, diagnostics redaction/path stripping); `tests/unit/test_isolation.py` probe
  shape, namespace-denied reasons, timeout/missing backend, run-time setup failure.

### T62 — case-set runner for real-world failure cases (C35)

- `python3 tests/tools/run_cases.py CASES_DIR [--json] [--keep DIR]` runs every `case.json`
  (`orcexe.case.v1`: `id`, `title`, `lane` standard|risky, `repo.files`, `task {goal, allow, done,
  notes}`, `executor {scenario, writes, final_text}`, optional `evaluator.rubric` (criteria list |
  `invalid` | `unavailable`), optional `auditor` (audit.v1 object | `invalid` | `unavailable`),
  `expect {accepted: false, caught_by: gate|l0|l1|audit, max_model_calls_before_catch}`) through
  the real `orcexe task` path in a child process: temporary repo committed from `repo.files`, an
  approved one-task plan routed to a fake Codex executor that applies `writes` on every attempt,
  one fake Claude wrapper answering L1 and auditor calls (told apart by the audit brief), a risky
  case's contract proposed and approved first. Homes, caches, state and TMPDIR live in the case's
  temporary directory. Missing reviewer parts default to passing (the case must be caught by the
  layer it claims).
- Recorded from the task ledger only: final state/reason, the first catching layer and check
  (`gate.failed` reason → gate, or l0/audit for their ids; `l0.failed` first failed check; first
  failing `l1.verdict` criterion; failing `audit.verdict` first finding), model calls (executor /
  L1 / auditor) in total and up to the catch, and whether an accepted ref or land command exists.
  `model_calls_before_catch` = reviewer (L1 + auditor) calls started up to and including the
  catching call; the executor call that produced the work is reported beside it, not counted.
  Table by default, `{"cases": [...], "summary": {total, accepted, mismatches}}` with `--json`;
  exit 0 only when every case matches its `expect`, else exit 1 naming each mismatch.
- Public examples in `tests/cases/examples/`: `empty-done` (gate `run_status`, empty diff with a
  "DONE" final text), `skip-test` (L0 `skip_added`), `wrong-file` (gate `outside_allow`); all
  caught with 0 reviewer calls. A literal `@unittest.skip` decorator is caught earlier, by the
  gate's `test_floor` skip count; `skip-test` therefore adds `unittest.skip` through
  `from unittest import skip` / `@skip(...)`, which only L0 sees.
- `tests/unit/test_run_cases.py`: ledger→layer mapping and call counting, case-format
  validation, the examples end to end, plus temporary L1-fail (l1 `d1`, 1 call), risky
  auditor-fail (audit, 2 calls) and an accepted control case reported as a mismatch.

### T61 — `plan --run` call budget, C29 "never accepted" matrix, `output_capped` and executor `model_mismatch`

- `orcexe plan --run PLAN_ID [--max-calls N] [--max-minutes M] [--resume]` (N positive integer, M
  positive number). The orchestrator counts every model call it starts in the invocation, around
  the call (never from tool output): each executor attempt, each L1 call including fallback
  candidates, each auditor call; failed, unavailable and invalid calls included. A call that would
  exceed N, or any call after M minutes of wall-clock time, is not started: the task in progress
  ends `interrupted` with reason `budget` (task reason table, exit 1; resumable; never accepted, no
  accepted ref, no chain advance), later tasks stay `pending`, the run ends `partial` (exit 1) with
  `stop_reason: "budget"` and `calls`, and the ledger records `plan.budget {plan_id, max_calls,
  calls, max_minutes (decimal text or null), elapsed_ms, reason: max_calls|max_minutes}`. A
  running call is never cut. `--resume` with a new budget continues; attempt numbers continue
  (no refund). Without either flag nothing is counted and the result is unchanged.
- Hole closed (cap_reached): every adapter (Codex, Claude Code, Cursor) adds the run warning
  `output_capped` when a stdout/stderr line exceeded the 1 MiB line cap; measured on main, a Codex
  `big-line` run with a real edit was `completed` with no warning and accepted in all three lanes.
- Hole closed (model_mismatch): an executor run with `model_mismatch` was accepted by `do` and
  `task`. `gate_lite.UNCLEAN_RUN_WARNINGS = (output_capped, model_mismatch)`: either warning fails
  `run_status` in `do` (detail = the warning) and in the standard/risky gate (a real failure of
  the attempt, not environmental/quota), and makes an L1 or auditor receipt unavailable.
  Standard-lane `gate_passed` for `l1_invalid`/`l1_unavailable` is unchanged (frozen ref + human
  land command).
- C29 matrix (`tests/unit/test_never_accepted_matrix.py`): rows empty_diff, quota, cap_reached,
  timeout, pipes_held, model_mismatch, l1_invalid, l1_unavailable, no auditor, budget × fast
  (`do`; budget via a fast-lane `plan --run` task), standard, risky; 26 applicable cells, each
  built independently with fake CLIs, none ends `accepted`. The module prints the observed matrix
  and the cell count (and writes JSON to `ORCEXE_MATRIX_OUT` when set).
- Changed tests: `CursorFamilyTests` route the model that must reach L1 (an off-route actual model
  now fails `run_status`); the full adapter contract expects `output_capped` for Codex `big-line`.

### T60 — minimal risky lane: `contract.v1`, per-task contract approval, cross-family auditor, `diagnosing`

- `risky` is a supported lane (K18 a; T57 r2 §3.1–§3.6, decision 027 Ş3). `plan --approve` defers a
  risky task as `no_auditor` (evidence `auditor: none (executor families: …)`) when no eligible
  cross-family auditor route exists at the decision; the live reason is frozen, never recomputed
  at dispatch, and `--open` recomputes it. Approvals made before T60 are `plan_stale`
  (`lanes_changed`); a proposal still carrying a risky `lane_note` is `plan_stale` at approval.
- Per-task contract approval: the first `task`/`plan --run` on an approved risky task writes
  `.orcexe/contracts/<plan>/<task>/contract.proposed.json` + ledger `contract.proposed
  {approval_sha256}`, `task.state → awaiting_approval` and exits 2 `contract_unapproved` (plan run:
  other tasks continue, dependants stay `pending`, run `partial`; run output and `status` print the
  exact approve command). New `orcexe task --approve-contract PLAN_ID TASK_ID APPROVAL_SHA256
  [--repo PATH]` (user only; README/SKILL rule) records `decision.v1 {kind: contract_approval}`,
  its `decision.recorded` anchor and `contract.approved`. A hash other than the current proposal's
  is `contract_unapproved` (`detail: proposal_changed`). The approval hash covers T57 §3.2's field
  list; `base` is outside it and recorded as `contract.frozen {approval_sha256, base}`. An
  unanchored approval line is `state_tampered` (`detail: contract_unanchored`, approval revoked).
  `--constraint TEXT` (≤5, ≤300 chars) re-proposes with human constraints.
- `orcexe.contract.v1` = contract.lite + `notes_sha256`, `risk {signals, areas}`, `forbidden_paths`
  (risk areas the allow globs do not cover + do-not-touch; the only paths that give
  `risk_path_touched` on risky), `executor`, `auditor` (upper bound), `l1_required_basis:
  cross_family`, `human_constraints`, `budget {attempts: 3, auditor_calls: 3, same_failure:
  diagnosing}`, `approval_sha256`, `base`.
- Order per attempt: executor → gate → L0 → L1 → auditor. Risky L1 skips `cursor_read_only` and
  `same_family` (`l1_basis_insufficient`). The auditor (`roles.yaml` `auditor` stage via
  `route.route`) is a separate read-only call in a throwaway copy; its family excludes
  `task_l1.executor_families` of the executor's tool and actual model at run time; its model must
  differ from L1's when another eligible model exists, else `same_model_as_l1: true` (ledger and its
  own land-form line). Output `orcexe.audit.v1` via `mapping.parse_strict_json_output`; `pass` only
  without P0/P1/P2; verdict from the adapter's in-memory final text. `audit_invalid` is asked once
  more without spending an attempt (second: `gate_passed`/`audit_invalid`); `fail` findings go to
  the next brief as untrusted feedback. Auditor writes to its copy → `rejected`/`auditor_wrote`;
  writes to ledger/runs/decisions/plan during its call → `state_tampered`/`main_repository_changed`
  (T55 runguard + main snapshot). Only `fail` verdicts are remembered per tree (also on `--resume`).
- `accepted` (risky) only when L1 `cross_family` passed, the auditor `cross_family` passed, the L1,
  audit and land trees are equal and isolation evidence exists; otherwise `gate_passed` with the
  reason. On risky the accepted ref and landing command are written only after that decision
  (standard lane unchanged). Same failure class twice (stage, check id, normalized message hash;
  environmental results excluded; recorded as `gate.failed {failure_class}`) → `diagnosing`
  (`same_failure`, `task.state {class, attempts}`), final, not landable.
- New ledger events/fields: `contract.proposed`, `contract.approved`, `audit.started`,
  `audit.verdict`; optional `contract.frozen.approval_sha256/base`, `gate.failed.failure_class`,
  `task.state.class/attempts`; the ledger accepts a `bool` field type. New task reasons:
  `contract_unapproved` (2), `l1_basis_insufficient`, `auditor_unavailable`, `audit_invalid` (0),
  `auditor_wrote`, `same_failure` (1). `planrun.FINAL` gains `diagnosing`; `awaiting_approval` is
  parked. A contract approval anchor is never counted as the plan's latest decision.
- Standard-lane fix: a model id also names its vendor family by prefix (`claude` → anthropic,
  `gpt-`/`o3`/`codex` → openai, …), so a Cursor executor running e.g.
  `claude-opus-5-thinking-high` never gets a Claude L1 counted as `cross_family`.
- r2 (hidden-gate findings): environmental outcomes never get a failure class — besides
  quota/timeout/stalled/auth statuses, `*_unavailable` and `audit_invalid`, an executor
  `run_status` failure caused by them (`quota_suspected`, `executor_error`) no longer counts, so
  quota ×2 then success reaches attempt 3 and is accepted. An unavailable auditor call is retried
  (next candidate, then the same again) within the 3-call budget before `auditor_unavailable`.
  `same_model_as_l1` is shown on its own line containing the literal `same_model_as_l1` in `task`
  text output, `plan --run`, `status` and `status.md` (`Auditor same model as L1:
  same_model_as_l1 (…)`), and as fields in `task --json` (`auditor.same_model_as_l1`) and the land
  form (`land.same_model_as_l1`).
- r3 (L2 review, RET narrow): (P1-1) the executor family set for L1 and auditor is the union of
  every attempt's `model_actual` (new optional `attempt.ended.model_actual`, re-read on `--resume`)
  plus the routed model; the L1 chain is recomputed each attempt; only failing L1 verdicts are
  remembered in-run. (P1-2) vendor tokens anywhere in an id (`sonnet`, `opus`, `anthropic/…`, `gpt5`,
  …) name the family; a multi-vendor tool's missing/`auto`/unrecognised slug is unknown → never
  `cross_family` (standard L1 is labelled `same_family`; risky gets `l1_basis_insufficient` /
  `auditor_unavailable`). (P1-3) audit `path`/`reason` must be single-line printable (`path`
  relative, ≤160) or the reply is `audit_invalid`; `task`/`plan --run` text output escapes every
  non-printable character. (P2-1) `environmental()` uses structured run results only (status,
  warnings, exact `*_unavailable` codes), never words in details or findings. (P2-2) risky:
  accepted record first, then `refs/orcexe/accepted/*` (no ref if the record fails). P3: `--resume`
  re-checks the contract approval (else re-proposes); `--open` recomputes the auditor for every
  risky task opened; `roles.yaml` `auditor` gains `claude-sonnet-5-5` and `gpt-6-sol` so the auditor
  differs from the L1 model when one is in access; failure classes are `stage:<32 hex>` (never cut
  or redacted in the ledger; readable check label separate); no `Auditor:` line on non-accepted
  tasks; `--constraint` on resume is refused (`detail: constraint_on_resume`).
- Tests: `tests/unit/test_risky_lane.py`. T59 tests that exercise `lane_not_supported` deferral
  now patch `planning.SUPPORTED_LANES` to `('fast', 'standard')` (the K18 b fallback).

### T59 — partial plan approval and `deferred` tasks; `--open`; classifier version check

- `orcexe plan --approve PLAN_ID [--defer T2[,T5]]` approves the runnable subset (T57 r2 §2,
  F11, K19 a). Runnability is computed and frozen at the decision: `lane_not_supported` (risky
  in this version), `no_route` (the plan's route is null), `deferred_by_human` (`--defer`) and
  `dependency_deferred` (`cause`: the first deferred dependency) for every transitive dependant;
  each entry also carries a short `evidence`. Nothing runnable (or an unknown `--defer` id) is
  exit 2 `plan_rejected` with one stderr line per task and nothing written. `no_auditor` joins
  with the risky lane (T60). The old whole-plan refusal `lane_not_supported` (exit 2) of
  `plan --approve` is gone from the plan reason table.
- `content_sha256` and `plan_id` are unchanged by a decision. `plan.approval = {approved,
  deferred, approval_sha256, decision_id, at, opened_from}`; `approval_sha256` hashes
  `{plan_id, content_sha256, classifier, head, approved, deferred, opened_from}`. The
  `orcexe.decision.v1` record (`plan_approval` / `plan_open`) carries the same lists and hash,
  and the ledger gets the new event `decision.recorded {kind, decision_id, plan_id,
  record_sha256}` (T55 head ref and session trust; written before the plan is published) plus
  `task.state pending → deferred` (approval) and `deferred → pending`, reason `opened` (open)
  rows. Approval/open take the dispatch lock (`task_running`), check the tamper marker and verify
  the ledger head first (`state_tampered`, `ledger_corrupt`, `ledger_head_mismatch` now in the
  plan table).
- Dispatch gate (`task._read_plan`, so `task`, `plan --run`, `--resume` and `--open`): after the
  content hash and HEAD, a `classifier` other than `classify.CLASSIFIER_VERSION` is `plan_stale`
  with stderr `detail: classifier_changed` (also when approving a proposal); a missing
  `approval` (plans approved before T59) is `plan_stale`; then `approval_sha256`, `approved ∪
  deferred` = the plan's tasks without overlap, at least one approved task, the plan-derived
  reasons recomputed (a fixed point of the human-held set, so a consistent forgery that approves
  a risky task still fails), the latest decision record (hash, lists, head, classifier, chain
  `opened_from`) → else `plan_tampered`. Each decision of the current chain needs its
  `decision.recorded` anchor with the record's canonical SHA-256, and every ledger
  `deferred → pending` row needs an anchored `plan_open` that opened it → else `state_tampered`
  with `detail: decision_unanchored` / `open_unanchored` and a "propose again" hint (no marker is
  written: the plan itself is untrusted, not the ledger). Dispatch permission is the approved
  list: `task` on a deferred task is exit 2 `task_deferred`; `plan --run`/`--resume` skip
  deferred tasks (run `partial`, exit 1). Status/ledger projections show them as `deferred` with
  their frozen reason, taken from the decision, not from ledger rows.
- `orcexe plan --approve PLAN_ID --open T3[,T4]` (approved plans only, else
  `plan_not_approved`): the dispatch gate plus the full approval-time `plan_tampered` checks,
  then runnability now — an opened task must be deferred, runnable and have every dependency
  approved or opened in the same call; otherwise `plan_rejected` with `refused: T<n> <why>`
  lines and nothing written. Remaining deferred tasks are re-reasoned (r3: a dependant whose
  dependency was opened but that was not opened itself keeps `dependency_deferred` with its cause,
  evidence `depends on T2; not opened with it`). Landing
  moves HEAD, so `--open` after a land is `plan_stale` (decision 027 Ş4).
- Land form: `land.deferred` lists the deferred tasks and `status.md`, `plan --run` and `task`
  print "Warning: landing closes deferred T3/T4 for this plan for good …". `plan.md`: "Will run
  after approval" / "Will be deferred" sections and the approve/defer/open commands; after a
  decision `Status: approved (2 of 4; 2 deferred)`.
- Docs: README partial-approval section (CLI, record, hash, gate order, open, land warning,
  threat-model boundary of decision 027 Ş2), task/planrun/plan reason tables (`task_deferred`;
  `plan_stale`, `plan_tampered`, `state_tampered`, `plan_rejected` meanings), SKILL workflow line
  and tables (two blank lines dropped to stay at 160 lines).
- r2 (§2.6): `plan.md` keeps the design's `## Will run after approval` (approved ids with lanes,
  ` · `-joined) and `## Will be deferred` sections after a decision too (r1 renamed them
  "Approved to run"/"Deferred"); a deferred entry reads `- T3: dependency_deferred (depends on
  T2)` when it has a cause, else `- T2 (risky): <reason> — <evidence>`. Pinned in the C33 test.
- r2 (§4 `approval.revoked`): new ledger event `approval.revoked {plan_id, reason}`. The first
  dispatch gate (`task`, `plan --run`, `--resume`, `--open`) that finds a decision without its
  anchor (`decision_unanchored`) or an unanchored opened row (`open_unanchored`) writes it, then
  refuses with `state_tampered` as before. Every later gate for that plan refuses with exit 2
  `plan_not_approved` and stderr `detail: approval_revoked` plus a re-approve hint (no new
  reason word). `plan --approve PLAN_ID` accepts an `approved` plan only when its latest anchored
  approval was revoked (otherwise `plan_not_proposed`), under the dispatch lock and only after the
  tamper-marker check and `verify_head` pass (`ledger_corrupt` etc. write nothing); it starts a new
  decision chain. Opened-task ledger rows count only after the current chain's anchored
  `plan_approval` (by ledger seq), so a forged row before a fresh approval no longer blocks.
  Not covered: a mid-run `state_tampered` from the T55 run guard (ledger/runs/ control state) does
  not revoke the approval — its ledger is untrusted until `ledger repair`, and the marker already
  stops every dispatch.
- r3 (L2 review T58-T59, T59 RET narrow):
  - P1-1, replaying an older, wider approval: every `plan --goal` publication first writes the new
    ledger event `plan.proposed {plan_id}` (dispatch lock, so never inside a running plan:
    `task_running`; written before publishing, fail closed). The gate trusts a decision chain only
    when its tail is the plan's LAST `decision.recorded` and its start comes after the latest
    `plan.proposed` (any plan); otherwise `state_tampered`, `detail: decision_superseded`, plus
    `approval.revoked`. Both L2 paths are refused before any dispatch: (a) same `plan_id`
    re-proposed and approved narrower, then the old plan.json and A1 line restored; (b) a replaced
    plan restored through plan.json alone. Tamper twins 6a/6b.
  - P2-5: after `approval.revoked` a fresh approval needs the deferrals restated:
    `plan --approve PLAN_ID --defer IDS` or `--defer none` (new parser value). A bare re-approval is
    `plan_rejected` with `refused: T2 deferred_by_human (restate with --defer T2 or --defer none)`,
    taken from the last genuinely anchored decision. Hints name the flag. I picked "restate"
    over "carry forward": it never decides for the human and does not trust a decision chain that was
    just tampered with.
  - P3-1: `plan --run` re-checks the approval (`task._read_plan`) after every dispatched task, so a
    decision forged in a task's executor window stops that run with `state_tampered` and revokes
    before the next task.
  - P3-2: revocation is decided from the ledger alone (`approval.revoked` newer than the plan's
    last anchor). Re-approval ignores a forged `approval` left in plan.json and recomputes from the
    content. The revoking gate republishes the plan as `status: revoked` (`plan.md`
    `Status: revoked (…)`), and `orcexe status` reports `plan_status: revoked` even when plan.json
    still says approved. A `revoked` plan is `plan_not_approved` / `approval_revoked`.
  - P3-3: `plan.md` keeps the §2.6 sections and offers `--open` only for openable tasks (deferred by
    the human, or only behind such tasks); the rest are listed as "Cannot be opened in this
    version". README says `--open` uses the plan's recorded routes, not live access.
  - P3-4: see `--open` above. P3-5: the approval records `lanes` (the runnable lanes, inside
    `approval_sha256`); a different set at dispatch or `--open` is `plan_stale`,
    `detail: lanes_changed`, not `plan_tampered`.
  - P3-6: `ledger repair` lines show `kind=` and `decision_id=` for anchors.
- Tests: `test_partial_approval` — C33 end to end with fake CLIs (fake Claude manager proposes 4
  tasks, the risky T2 and its dependant T3 deferred, T1/T4 run by the fake Codex executor and
  accepted, run `partial`, land warning, `--resume` dispatches nothing), `--defer` and
  nothing-runnable refusals, parser checks, `task_deferred`, lock, `--open` chain/anchor/refusals,
  `plan_not_approved`, `plan_stale` after landing, classifier change for approve/`--open`/
  `plan --run`/`task`, pre-T59 plans, tamper twins 1, 1b (consistent forgery incl. ledger
  anchor), 2, 3, 4, 4b (forged opened row), 4c (edited record), 5 and a tampered `--open` (each
  unanchored twin also checks the revocation and the follow-up `plan_not_approved`); revocation
  end to end (`plan --run`/`task`/`--open` refused, corrupt ledger refuses re-approval and writes
  nothing, bare re-approval refused until `--defer none`, fresh approval runs both tasks, an
  unrevoked plan stays `plan_not_proposed`); r3: twins 6a/6b, status/plan.md `revoked`,
  re-approval over a forged plan file, `--open` suggestions, `lanes_changed`, repair line, and a
  decision forged in the executor window stopping the run;
  deferral fixed point; decision overlay. Fixture: `TaskFixture.save()` now writes a real T59
  approval with its anchor (`write_approved`), and its `log()` view hides that anchor so
  lifecycle assertions keep their exact event lists.
- Changed assertions (r3): `test_planrun` stacked-order test expects `_read_plan` calls
  `1 + dispatched tasks` (was 1; the P3-1 re-check); the `--open` re-reasoning test expects
  `dependency_deferred`/cause `T2` (was `deferred_by_human`); `test_planning` expects
  `- T3: dependency_deferred (depends on T2)` (§2.6 form, r2).
- Changed assertions: `test_task` precondition cases `lane_not_supported`/`no_route` now expect
  `task_deferred` (a runnable task was added so the plan can be approved); `test_planrun` T5/T6
  are `deferred` (`lane_not_supported` / `dependency_deferred`) instead of `blocked`, and the
  plan-wide `no_route` precondition case was removed (a routeless task is deferred);
  `test_planning` unsupported-lane form expects the §2.6 sections, the approval test now expects
  partial approval and `plan_rejected` only when nothing runs, and the unmarked legacy risky plan
  is approved with T2/T3 deferred; `test_isolation` saves its post-tamper plans without the
  ledger anchor (the ledger is deleted or forged there; the marker decides first).

### T58 — lane classifier v2: goal + done + notes, the lane only rises; a broken risk map never lowers

- `classify.task_lane(goal, done, notes, files)` (T57 r2 §1.2, decision 027 Ş1). The goal keeps
  every `classify()` rule (union), so `lane_v2 >= lane_v1` for every goal; K-2 narrows identifier
  hiding: a stem is hidden only by an identifier that resolves to a tracked file (full path or
  basename with extension, `:line` suffixes dropped) outside call context (a run verb anywhere in
  the sentence, a `(`, or a `.sh .bash .zsh .ps1 .bat .cmd` extension). Dotted names hide only the
  tracked `.py` module part; `word(s)`, `word(es)` and `word(1)` are words, not calls (F10, P2-4).
- Action-shape rules A1–A8 apply to every field: command spans (spaced backticks, double
  backticks, fences; no length or `. ` exception), calls, command names, EN/TR imperatives,
  run verb + outward part, hard destructive words (`rm rmtree rmdir unlink shred mkfs truncate
  purge wipe erase`, `reset --hard`, `git clean`, `drop table`, `delete from`, `yok et`,
  `find … -delete`), uppercase HTTP methods, and directed verb forms ("is pushed to origin").
  Done items and notes add the narrow exact-word risk subset (K-4); other risk words are recorded
  as information only, soft destructive words keep `standard`, and only the goal can yield fast.
  Words from a project's `risk-map.yaml` also raise in done/notes.
- Signals name their field (`goal:A2`, `done[1]:A8`, `notes:K4`, `notes:risk_word`);
  `lane_basis` lines become `cli:done[1]:A8:pushed` etc. Today's goal signals keep their ids.
- `classify.CLASSIFIER_VERSION = "lane.v2"`; `orcexe plan` computes every task lane with
  `task_lane` (via `task_lane_repo`) and records `"classifier": "lane.v2"` in the plan (bound by
  `content_sha256`). The version check at approval/dispatch is T59.
- P1-3: `_blocked` no longer lowers a text-derived `risky` to `standard` when
  `.orcexe/risk-map.yaml` is unusable (corrupt, wrong type, unknown key, unsupported version,
  dangling symlink, unreadable; `TypeError`/`RecursionError` are now caught too); every unusable
  input floors the lane at `standard` and never yields fast. `test_classify` previously asserted
  `standard` for `auth README.md` with a broken map; it now asserts `risky`.
- Deviation: the interpreter names `sh bash zsh node npx` in the run-verb list match whole words
  only (as prefixes, `sh` turned every "should"/"show" sentence into call context).
- Tests: `test_classify_v2` (every A-rule in done and notes, quiet twins, the P2-4 nine, call
  forms, dotted names, F10 nouns, uppercase/Turkish inflections, backtick rules, risk-map floors,
  and the `lane_v2 >= lane_v1` differential over today's test texts); `test_planning` field basis
  and recorded classifier version.

### T55 — ledger authority: writes during a run are tamper, a CAS head ref, a repairable tail

- P2-2: new `runguard.Guard` observes, before and after every external model run, the ledger
  bytes (size + sha256), the head ref and a fingerprint of the dispatch's run directory. `task`
  wraps the executor `adapter.run` and every L1 evaluator call (`guard.wrap(adapter)`; the flag
  is sticky because `task_l1.evaluate` reports exceptions as unavailability); `do` wraps its
  executor; later auditor calls use the same helper. Any ledger growth or change, a moved head
  ref, or any run-directory change other than the adapter's fixed artifacts (`.attempt`,
  `brief.md`, `brief/brief.md`, `final.md`, `events.jsonl`, `stderr.txt`, `result.json`) in that
  call's own output directory is terminal `state_tampered` (`task`: `rejected`, exit 1; `do`:
  `failed`, exit 1; nothing landed) and writes the persistent `.orcexe/state-tampered` marker.
  `plan --run` stops at a task that ends `state_tampered` (exit 2), so dependants of a forged
  `task.state … accepted` record are never dispatched.
- Head anchor: a ledger at `<repo>/.orcexe/ledger.jsonl` of a Git checkout keeps
  `refs/orcexe/ledger-head`, a blob `orcexe.ledger.head.v1` / `seq N` / `sha256 <last line>`.
  Under the existing ledger lock every append first requires the ref to match the current tail,
  then moves it with `git update-ref <ref> <new> <old>`; a ref moved by someone else in between
  is refused (`ledger_head_mismatch`), never overwritten. `plan --run`, `task`, `status` and `do`
  call `Ledger.verify_head()` (whole chain + head) before deciding anything; a mismatch is exit 2
  `not_seen: ledger_head_mismatch` with a repair line. A ledger without a ref (written before T55)
  is adopted once, after its whole chain verifies. Suffix deletion and last-line edits, invisible
  to the chain alone, are now detected. The head ref is excluded from the main-repository ref
  snapshot (`main_repository_changed`), since the CLI itself moves it during a run.
- Corrupt tail (T32): a ledger with an invalid line is exit 2 `not_seen: ledger_corrupt` plus a
  line naming `orcexe ledger repair` for `task`, `plan --run`, `status` and `do` (was `do` exit 70
  "Internal error: truncated_tail", `status` `state_corrupt`). New `orcexe ledger verify [--repo]
  [--json]` (exit 0; 2 with `ledger_corrupt` / `ledger_head_mismatch`) and `orcexe ledger repair
  [--repo] [--json]`: keeps the longest valid chain prefix, moves the rest byte-for-byte to
  `.orcexe/ledger.quarantine-<UTC>.jsonl` (0600, never overwriting an earlier one) before
  truncating, re-anchors the head ref and prints what it moved. It never changes a valid ledger
  whose head matches; for a head mismatch on a valid ledger it prints what the ref said and what
  the ledger ends with, then re-anchors. Repair takes the dispatch lock.
- Junk lock: an empty or unparseable `do.lock` (or one without a `pid`) older than
  `do.JUNK_LOCK_GRACE_S` (10 s) is reclaimed with a stderr warning; the dead-pid reclaim and the
  refusal of live, remote and fresh locks are unchanged.
- Docs: `ledger_corrupt` / `ledger_head_mismatch` rows (exit 2) in the task, plan --run, status
  and do reason tables (README) and the shared SKILL table; a README `ledger` section.
- Tests: `test_ledger_authority` — head moves with every append;
  suffix deletion, last-line edit and deleted ledger detected (chain alone still verifies); old
  ledger adopted once and only when its chain verifies; CAS race refused with the foreign ref
  kept; corrupt tail exit 2 + hint, repair → quarantine file (exact bytes, 0600) and verify ok;
  mid-file corruption; valid ledger untouched; guard allows adapter outputs and flags foreign
  writes; junk lock reclaim; executor-time and L1-time forged records and planted run files →
  `state_tampered`; a plan run never dispatching dependants of a forged acceptance.

#### T55-r2 — L2 review (Fable, RET narrow): P1-1, P2-1..P2-3, P3-1..P3-5

- P1-1: the L1 verdict is parsed from the final text the adapter captured in memory when the
  evaluator run ended (`Adapter.final_text(run_id)`, set by base/Claude/Cursor adapters before
  `final.md` is written as evidence); `l1/final.md` is never re-read, so a process the executor
  left behind (`setsid` grandchild) rewriting it cannot turn a rejection into acceptance. Same
  principle: L0 reads runner counts from the gate's in-process log copy
  (`gate_lite.project_log`), not `gate/<id>.log`; the map/plan manager uses the captured text
  (disk only for a run that captured none, i.e. a mocked adapter).
- P2-1: per-command ledger trust (`ledger.session` / `in_session` on `task`, `plan --run`,
  `status`, `do`). The ledger remembers the last `(seq, sha256, size)` this command wrote or
  verified; any other tail at a later append, read or verify is `ForeignRecord` →
  `state_tampered`, also between guarded windows and with the head ref moved.
- P2-2: the `state-tampered` marker is JSON (`orcexe.tamper.v1`) carrying the last trusted
  ledger point (guard, foreign record and gate-time poison). `finish()` cannot append on top of
  an untrusted tail. `ledger repair` keeps the trusted prefix (marker point, else the head
  ref's record, else a never-anchored ledger's valid prefix), quarantines and prints every
  later record, and refuses unvouched anchoring (ref naming no record, deleted ref of an anchored
  ledger, marker without a point) unless `--accept-unanchored`; the marker stays for the human.
  With the marker present `status` and `ledger verify` exit 2 `state_tampered` (new status row).
  README documents the recovery steps.
- P2-3: `Guard` fingerprints the whole `.orcexe/runs` tree (other dispatches included), and the
  L1 window also compares the executor window's main-repository snapshot (`_main_snapshot`:
  config, plan/plan-state, decisions, refs, status) → `main_repository_changed`.
- P3-1: a `ledger.anchored` record (hidden from ordinary readers, `include_meta=True` shows
  it) is written at first anchoring; a missing ref of such a ledger is `ref_deleted`, never
  re-adopted. Remaining limits are documented (empty + ref deleted, packed-ref fallback +
  truncation, consistent rewrite between commands).
- P3-2: only empty/unparseable/non-object locks are reclaimed after the grace; `{}`,
  `{"pid": null}` and other ownerless objects block with a warning naming manual removal.
- P3-3: the ledger is opened with `O_NOFOLLOW` (append, read, verify, repair); a link is
  `ledger_corrupt` and its target is untouched.
- P3-4: a CAS-rejected append truncates its line again under the lock (a short write still stays
  visible as before).
- P3-5: README repair exit-2 cases, `state_tampered` meanings and recovery steps.
- Tests: survivor (`setsid` grandchild) and in-window `final.md` rewrites keep the L1 rejection;
  between-window forgery (ref moved) → `state_tampered`, T2 not dispatched; recovery
  quarantines the forged record and status refuses while marked; L1-time `decisions.jsonl` /
  `plan.json` writes → `main_repository_changed`, other dispatch's run dir → `state_tampered`;
  deleted ref after anchoring; records past the ref quarantined, `--accept-unanchored`; marker
  trust point; marker without point; linked ledger; session trust; ownerless locks; gate log
  memory. Reviewer probes rerun against the worktree: A/D L1 rejection kept (exhausted), B and C
  `state_tampered` with the forged record quarantined, J1/J2 `main_repository_changed`, J4
  `state_tampered`, E/F/G/H/I fail closed.
- Changed assertions: L0 fixtures write their log through `gate_lite._write_log` (the gate's own
  writer) instead of the file; `test_ledger_authority` adapted to the `ledger.anchored` record
  (sequence numbers +1) and to repair refusing unvouched anchoring.

### T54 (real run A, plan tasks T1–T2) — `answer --choice` takes a label or an index; scanned Python parses without warnings

- `answer --choice QID=VALUE` accepts an option label or a zero-based index. A value of digits
  that is in range is always read as an index, even when it equals another option's label; an
  out-of-range digit string is matched as a label. Any other value is invalid: exit 2, stderr
  `not_seen: answer_invalid`, then `valid choices for QID:` and one `  <index>: <label>` line per
  option, and nothing is appended to `decisions.jsonl`. Other `answer_invalid` cases (unknown
  question, empty `--text`, repeated or already decided id) keep the bare reason line.
- `gate_lite.parse_python` wraps `ast.parse` with `SyntaxWarning` and `DeprecationWarning`
  ignored inside the call only. Slop, the gate (`gate_lite._python_code`) and L0
  (`task_l0._python_tests`) parse scanned Python through it, so an invalid escape such as `"\d"`
  in scanned source no longer prints a warning. Trees and findings are identical to those for
  warning-free source, and a real `SyntaxError` is still raised and handled as before.
- Tests: `test_questions` label/index record equality, numeric-looking labels preferring the
  in-range index, and the `valid choices for QID:` hint after the reason for `Nope`, `first`,
  `9`, `-1` and an empty value; `test_ast_warnings` for empty stderr at every call site,
  results matching warning-free source, tree equality with a plain parse, `SyntaxError` still
  raised, and the filter staying scoped to the parse call.

### T54c — no deprecated ast nodes on 3.12; docs-only diffs pass L0 red-first

- `slop._constant` checked `ast.Num` / `ast.Str` / `ast.NameConstant` alongside `ast.Constant`;
  on Python 3.12 that emits `DeprecationWarning` (failing `test_ast_warnings` and filling the
  gate's 4 KiB log tail, finding F1). Python 3.9+ parses every literal as `ast.Constant`, so the
  check is now `ast.Constant` only, behaviour unchanged. No other product use of the deprecated
  nodes remains under `plugins/orcexe/src/orcexe/` (`gate_lite.py` had none).
- Real run A task T3 (only CHANGELOG.md, README.md, SKILL.md changed) failed L0 three times with
  `red_first_missing: no_observed_failure_on_base`: `tests_missing` exempted docs-only diffs but
  red-first did not. New `task_l0.docs_only(paths)` (non-empty, every path `is_doc`, none
  `is_test`); red-first now passes with detail `docs_only` for such diffs. Any diff touching a
  code or test file still needs a test that fails on base; all other L0 checks still run.
- Tests: the fast doc-only case now expects red-first `pass`/`docs_only` (was `fail`); new cases
  for docs-only (passes), doc + code without a red test (fails), doc + test passing on base
  (fails), and the `docs_only` predicate.

### T52c — runner summary trusts orcexe's own aggregate line (T52a regression)

- Real run A lost every attempt to L0 `test_count_dropped` / `runner_count_not_seen`: T52a
  trusted a last summary only when it equalled the sum of the earlier ones, but
  `tests/tools/run_tests.py` prints only some module unittest blocks before its aggregate
  (17 visible blocks summing to 354 vs. `Ran 991 tests in 62 modules ...`).
- `task_l0.runner_summary`: when the text holds exactly one orcexe runner aggregate line
  (`Ran N tests in M modules in Xs; failures=F errors=E skips=K; failed modules=D`) and no
  summary of any format follows it, the aggregate is authoritative (count N, skips K, all
  counted as `skipped`); module blocks before it are ignored for counting. Two or more
  aggregate lines (including one a test prints early), or any summary after the aggregate,
  give count/skips `None` (not_seen). Without an aggregate line T52a's rule is unchanged;
  pytest single-line summaries are unchanged.
- Tests: tails of the three real run A logs (gate 991/19, L0 base 988/19, L0 red 991/19 with
  failures=3) as fixtures under `tests/fixtures/runner-summary/`, plus forged trailing
  summaries, forged second/early aggregates, plain unittest and pytest cases.

### T52b — `.orcexe` pre-check, usage exits for bad repo/task text, L0 test-command docs

- Shared `fsio.require_main_orcexe` runs before any lock, ledger write, worktree or branch
  in every writer (`do`, `task`, `plan --run`/`status`, `run`, `trust`, `map`, `plan`,
  `questions`, `answer`, `intake`, `slop`, `history`). A symlink, dangling link or regular
  file at `.orcexe` exits 2 with a remove/rename message; nothing is created. The check is
  additive: `run` still refuses symlinks at `.orcexe/runs` and the run directory, `intake`/
  `history` at `.orcexe/intake`, and `map` re-checks `.orcexe` before its first write.
- Reasons: `do` reports it as a usage message; `map`, `plan`, `questions`, `task`,
  `plan --run` and `status` report `orcexe_invalid` (replacing `output_symlink` for this
  case; `planrun`/`status` no longer emit `output_symlink`); `intake`/`history`/`slop`
  keep `output_symlink` (exit 2); `trust` names `.orcexe` in its exit-2 message.
- `do`: a `--repo` path that does not exist exits 2 (was 70); an existing directory that
  is not a repository keeps the everyday-mode exit 7. Task text that is not encodable
  UTF-8 (lone surrogates) exits 2 before any ledger write (was 70 with an orphan
  `task.state`). `task`/`plan --run` map a missing `--repo` to `not_a_repository` (2).
- README and SKILL document the L0 test-command id/argv rule and `orcexe_invalid`.

### T52a — gate environment: declared interpreter (F5), runner summary (P3-1), L1 copy secrets (P3-3)

- F5: project checks run with the project's declared Python, not whatever `python3` PATH has first.
  The declaration is read from the main repository / base commit only (never the executor's
  worktree): main `.orcexe/commands.yaml` top-level `python: "3.9"` (new optional key,
  `major.minor` string only), else the base `.python-version` (first `major.minor`), else the lower
  bound of base `requires-python` (pyproject.toml) / `python_requires` (setup.cfg). Resolution
  probes `python3.X` on the gate PATH, then `/usr/bin`, `/usr/local/bin`, `/opt/homebrew/bin`
  (`python3.X`, `python3`), running each inside the project sandbox with the existing timeouts and
  keeping the first whose `sys.version_info[:2]` matches. Commands whose argv[0] is `python3` or
  `python` (defaults and commands.yaml; gate, standard gate and L0 runs) use the resolved absolute
  path; other argv[0] are untouched. Declared but not found: `project_checks` is `not_seen` with
  `interpreter_unavailable: python X.Y` (also when no python command exists), never a pass. An
  invalid `python:` value is a `commands.yaml.python` config error naming the interpreter. No
  declaration keeps today's PATH `python3`. `interpreters.log` adds
  `project python: <path> version <X.Y> source <commands.yaml|.python-version|requires-python|PATH>`.
  `.python-version` joins the protected command sources.
- P3-1: `task_l0.runner_summary` no longer takes the last summary blindly. One summary is used as
  is; with several, the last is trusted only if its count (and skip total, when the earlier ones
  are known) equals the sum of the earlier ones, as `tests/tools/run_tests.py` prints; otherwise
  count/skips are not_seen. `skips=N` on that aggregate line is parsed, so skip counts are measured
  for orcexe's own runner. pytest single-line summaries are unchanged.
- P3-3: the L1 read-only copy also drops every path the intake secret rule (`intake._secret`)
  classifies as secret (`.env*`, `secrets*`, `*.pem`, `*.key`, `id_rsa*`, `*.p8`, `*.p12`,
  `credentials*`, `.npmrc`, `.pypirc`, `.netrc`, and their descendants). The L1 brief labels the
  gate and L0 blocks `UNTRUSTED GATE RESULTS DATA:` / `UNTRUSTED L0 RESULTS DATA:`.

### T51 — L1 fallback chain (K17), tree → verdict memory (P2-3), `l1.basis`

- K17 chain from one access snapshot (`task_l1.evaluator_chain`): every eligible evaluator of
  another family, then Cursor read-only, then another model of the executor's family. The
  executor's `model` and `model_actual` are never candidates. Cursor sits behind
  `task_l1.CURSOR_REVIEW_VERIFIED = False` (C16: its review profile is not measured on the real
  tool); a test pins it off, and the Cursor review run itself is not built.
- Run-time fallback: an evaluator run that ends unavailable (quota, auth, timeout, stall,
  non-zero exit, `pipes_held`, `model_mismatch`) hands over to the next candidate in the same
  attempt, each at most once, without re-probing; every call writes its own `l1.started` and
  `l1.verdict` (later calls use `attempt-N/l1-K/`). Malformed output (`l1_invalid`) stays
  terminal. No candidate left still ends `gate_passed` + `l1_unavailable`.
- `l1.started` / `l1.verdict` carry `basis` (`cross_family` | `cursor_read_only` |
  `same_family`) and `call`; `l1.verdict` also carries the evaluated `tree`. The task summary has
  `l1: {basis, model, reused}` for the passing verdict (null otherwise) and the text form prints
  `L1 basis: …`. All new ledger fields are optional; old ledgers still read.
- P2-3: within one frozen contract (also after `--resume`, rebuilt from the ledger), a rubric
  verdict for a tree is final. A later attempt with the same tree reuses it without calling L1
  (`l1.verdict` with `reused_from`; a remembered rejection gives the same feedback, read back
  from the stored rubric). `l1_unavailable` / `l1_invalid` are not remembered; the project gate
  and L0 always run again.
- L2 r2 (P2-1): the land ref can carry several tasks' commits, so the basis is derived per accepted
  task from the ledger (`planrun.task_records` → `l1_basis`, the passing `l1.verdict` of the
  accepting attempt, asked or reused). `land.l1_basis` is `{tasks: [{task_id, basis}], weakest}`
  (order cross_family > cursor_read_only > same_family > unknown > none; `unknown` = a pre-T51
  passing verdict without basis, `none` = accepted without L1, e.g. fast lane). `plan --run` JSON
  tasks carry `l1_basis`; `status.md` has an `L1 basis` column and a weakest-basis line.
- L2 r2 (P2-2): memory rebuilt from the ledger on `--resume` keeps only rejections whose criteria
  are exactly `d1..dN` for the contract; a remembered pass is never reused from the ledger (the
  same tree is asked again). Within one run both stay remembered.
- L2 r2 (P3-1): the executor's family set is its tool family plus the family of every tool that
  lists its `model`/`model_actual` (live or in the priors), so a Cursor executor running a Claude or
  GPT model is not `cross_family` against that vendor. `cursor.yaml` says `family: cursor`, which is
  right for Cursor's own `composer-2.5`.
- L2 r2 (P3-5): `roles.yaml` `as_of` is 2026-11-22 (pins in `tests/unit/test_route.py` updated).
- Changed assertions: `tests/unit/test_task.py` summary key set now includes `l1`;
  `tests/unit/test_route.py` `as_of` / `roles_as_of` pins 2026-11-17 → 2026-11-22.
- Shipped `roles.yaml` evaluator list gains same-family alternates (`claude-opus-5-5`, `gpt-6-sol`) after the cross-family pair, so K17 step 3 is reachable with shipped data (main session).

### T50 — plan-aware ledger KPIs (K7 split) and `questions.form`

- `ledger.kpis` keys tasks by `(plan_id, task)`, attributes `attempt.ended` via
  matching `attempt.started` `run_id`, and separates **gate_pass_rate** (per gate
  evaluation: `gate.result`/`gate.passed`/`gate.failed`) from
  **task_acceptance_rate** (dispatched keys whose last `task.state.to` is
  `accepted`). Removed misleading **acceptance_rate** (a gate-accepted task could
  still end `failed`). Added **l1_calls**, **questions_per_plan** (from
  `questions.form`, deduped by `form_sha256`); lane denominator is
  classified ∪ dispatched.
- New ledger event `questions.form` (`count`, `form_sha256`); `orcexe questions`
  appends one record after publishing `form.json` (ledger write failures do not
  change command output).
- Tests: `tests/unit/test_ledger_kpis.py`, updates to `test_ledger.py` and
  `test_questions.py`.

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
