---
name: orcexe
description: Use when the user wants another AI coding tool (Codex, Claude Code, Cursor) to do a small, well-scoped change in this git repository and have it checked before landing, or asks to check whether orcexe/the AI CLIs are ready here ("orcexe doctor", "is codex set up", "run this with codex").
---
# orcexe
## Workflow
Call `python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" COMMAND` from the repository; use `--repo` where supported and `--json` for parsing. Run `doctor` before the first `do` or for setup; `--quick` skips quota/app-server probes. Use `probe TOOL detect|auth|models|quota` for details. Never install or authenticate a tool without the user's request.
Use `map` → `questions`/`answer` → `plan --goal TEXT` → human review → `plan --approve PLAN_ID` → `plan --run PLAN_ID` (or `task --plan PLAN_ID --task T1`) → `orcexe status` → review frozen refs and land manually. `--resume` continues interrupted plans without replenishing attempts. `plan --run PLAN_ID --max-calls N --max-minutes M` (T61) counts every model call the run starts (executor, each L1 candidate, each auditor call, failed ones too); before a call that would exceed N, or after M minutes, nothing new starts: the task in progress stays `interrupted` (`budget`, resumable, never accepted), later tasks stay `pending`, the run ends `partial` (exit 1) with `stop_reason: "budget"`, `calls` and ledger `plan.budget`; `--resume` with a new budget continues without refunding attempts. An executor run with `output_capped` (output over the line cap) or `model_mismatch` (not the routed model) fails `run_status` in every lane; never accepted. `do "TASK"` is the fast single-target comments/docs lane; standard and risky work needs a plan. `plan --approve PLAN_ID [--defer T2,T5]` approves the runnable subset and freezes the rest as `deferred` (`lane_not_supported`, `no_route`, `no_auditor`, `deferred_by_human`, `dependency_deferred` with `cause`; dependants follow) into `approval_sha256`, anchored in the ledger by `decision.recorded`; nothing runnable is `plan_rejected`. `plan --run`/`--resume` skip deferred tasks (run ends `partial`), `task` on one is `task_deferred`. `plan --approve PLAN_ID --open T3[,T4]` opens deferred tasks with a new chained decision (dependencies approved or opened together; still unrunnable is refused, nothing written). Landing moves HEAD, so deferred tasks close for good (the land form warns; `--open` is then `plan_stale`). A changed classifier (`detail: classifier_changed`) or runnable-lane set (`lanes_changed`), or a plan approved before partial approval is `plan_stale`; edited approval lists/hashes are `plan_tampered`; a decision without its ledger anchor, or one that is not the plan's last anchor or predates the latest `plan.proposed` (every `plan --goal` writes it), is `state_tampered` (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`/`decision_superseded`) and writes `approval.revoked`: later dispatches are `plan_not_approved` (`detail: approval_revoked`, status `revoked`) until `plan --approve PLAN_ID --defer IDS|none` (deferrals must be restated) on a verified ledger. Same-user processes rewriting `.orcexe/`, ledger and refs consistently are outside the v0.3 threat model. Agent completion claims never establish acceptance. Risky lane (T60): the first `task`/`plan --run` on a risky task writes `.orcexe/contracts/<plan>/<task>/contract.proposed.json` + ledger `contract.proposed`, parks it `awaiting_approval` and exits 2 `contract_unapproved` (plan runs continue, dependants stay `pending`, run `partial`; `status` prints the exact command). **Only the user types `orcexe task --approve-contract PLAN_ID TASK_ID APPROVAL_SHA256` and `plan --approve … --open`; never propose, suggest running or run them yourself** (orcexe cannot check who typed them). After approval a plain `task`/`plan --run` freezes `contract.v1` and runs executor → gate → L0 → L1 (`cross_family` only, else `l1_basis_insufficient`) → a separate read-only cross-family auditor (`orcexe.audit.v1`, strict; family from the executor's tool and actual model; a model other than L1's when one is eligible, else `same_model_as_l1` on its own land-form line). `audit_invalid` is asked once more free, twice is `gate_passed`; `fail` findings feed the next attempt; the auditor writing is `auditor_wrote`; the same failure class twice is `diagnosing`/`same_failure` (quota/timeout/stalled/auth/`*_unavailable`/`audit_invalid` and run failures they cause never count; the task continues). No accepted ref or landing command unless `accepted`. `--constraint TEXT` (≤5) re-proposes before approval.
Plan lanes use classifier `lane.v2` (recorded in `plan.json`): goal, every done item and the notes are classified and the task gets the highest lane; outward or destructive action shapes in any field (commands, calls, `git push`, "is pushed to origin", `rmtree`) make it risky, and an unusable `risk-map.yaml` never lowers a lane. Words the classifier does not know (for example `aws s3 sync`, `pip install`) are not recognized, so still read each task before approving.
Map is incremental from `intake_sha`; missing/invalid maps, nonancestor HEAD, `--full` or over 40% changed files force a full rebuild. No-change maps skip models. Human JSON additions persist as `source: human`, deletions persist, restorations become proposals. Edited prose is kept with adjacent proposals and `human_edit_kept`. Current-state is always regenerated from CLI facts; retain all human blocks verbatim, including malformed tails, and save outside-block edits once to `current-state.previous.md`, reporting `current_state_previous_saved`. `questions [--goal TEXT]` saves a form; `answer --form ID --choice QID=INDEX|LABEL --text QID=TEXT` records validated batches (index is zero-based; an unmatched choice lists the valid choices on stderr after `not_seen: answer_invalid`). At most four answers per normalized goal; decided question IDs apply globally. Pending questions block planning. Plan P3 validates scope, dependency graph, sizes S/M, 1–12 tasks, shared bounded globs and test paths; plans require matching HEAD, canonical digest and approval before execution.
Standard tasks freeze `contract.lite.json` (scope, done criteria, base, three shared attempts) before dispatch. Candidate checks precede L0 test-count/skip/assertion checks and red-first tests on the base. L0 treats a `commands.yaml` entry as a test command when `id` plus joined `argv` matches `test`, `tests`, `pytest`, or `unittest` at word boundaries (`task_l0.test_commands`); only those commands count for red-first and runtime test-count checks. An independent L1 evaluator judges each done criterion, preferring another family and excluding the executor model/Cursor. Failed gates/rubrics feed the next attempt; invalid/missing evaluation leaves `gate_passed`. Gates check the cumulative tree against the task base: a retry reporting `empty_diff` (work already done) is gated on that tree, while a first-attempt `empty_diff` or a never-changed tree still fails. Project-check failures in feedback name the check and detail (e.g. `unittest: timeout (600s)`) so the executor re-checks rather than rewrites. Scope/risk/control violations are terminal; contract widening is forbidden. Status reads ledger facts without calling models; task reasons do not change a successful status read's exit 0. L0 and `slop` share test-check detection: `assert`, assertion calls (`assert`/`expect`/`should`/`verify` prefixes on any receiver), `fail`, exception contexts, `raise`/`throw`, XCTest and Swift `#expect`/`#require`. Hollow bodies still fail; slop retains constant-only/product-only precision signals. Dead exports ignore same-file uses and Python scripts (shebang/main guard, scripts/bin/tools, or unimported non-package files). Deno URL/specifier imports and deno.json/import_map.json mappings are declared; repository Python modules (including literal sys.path directories) are local. Python manifest/lock and Node package-lock/shrinkwrap declarations count; absent third-party imports remain findings. All scans are offline over bounded HEAD data.
Gate/L0/red-first steps get fresh temp HOME/scratch: Seatbelt on macOS, bwrap on Linux, network off, writes only to checkout/scratch and runner devices. Missing/denied sandbox means `isolation_unavailable`: task `gate_passed` (0), do `unverified` (1); no full acceptance. CLI control-state tampering is terminal `state_tampered`; investigate and restore trusted state before removing its marker. Ledger records or `runs/` files written while an executor/L1 evaluator runs, and any ledger tail a command did not write itself, are `state_tampered` too (the marker records the last trusted record; `status` then exits 2). The ledger head is anchored by `refs/orcexe/ledger-head`; `ledger_corrupt` / `ledger_head_mismatch` (exit 2) mean run `orcexe ledger verify`, then `orcexe ledger repair` (quarantines everything after the trusted record and prints it; refuses unvouched records unless `--accept-unanchored`). After `state_tampered`: repair, inspect, remove `.orcexe/state-tampered`, resume. Claude manager/evaluator uses restricted safe mode; a Claude **executor** loads the user's own Claude hooks/plugins. Codex/Cursor read their own user config.
Main `.orcexe/` stores map/intake/reports/forms/decisions/plans, ledger, `current-state.md`, `status.md`, private contracts/attempt evidence in `runs/`, publication generations/backups/proposals, locks and archives. State defaults to `~/.cache/orcexe` (`env.json`, manager runs/runtime/gate scratch); relocate with absolute `ORCEXE_STATE_DIR` outside the repo. Sibling `<repo>-wt/` holds executor worktrees. Git adds `orcexe/…` branches, `refs/orcexe/accepted/…`, `refs/orcexe/plans/…`, worktree metadata/receipts and an `info/exclude` entry. Temporary copies/HOMEs are removed normally. orcexe itself writes nothing else in `~`; tool config effects and OS-owned runtime caches (such as macOS system Python caches) are separate. Codex may append project trust in `${CODEX_HOME:-~/.codex}/config.toml`; Cursor creates project trust/runtime dirs and its five saved model keys are restored. `trust list` and `trust clean` preview recorded entries; `trust clean --apply --yes` needs the user's approval of candidates and writes a backup. Until v0.5 `clean`, stop runs, save changes/evidence, inspect `git worktree list`, remove exact worktrees with `git worktree remove`, then review/delete their branches/private refs. Remove chosen `.orcexe/` and resolved state files only after preserving approvals/resume history; state may be shared. Review receipts/exclude entries and empty sibling folders; inspect Cursor dirs separately. Never delete entire user tool configs. README lists all locations and manual commands. Manual `run --tool TOOL --model MODEL --effort EFFORT --brief FILE --workdir LINKED_WORKTREE [--profile review|write]` has no gate and requires the user's chosen parameters. Write refuses main. Report observed status, requested/actual model, changed files and evidence. Never pass API keys/billing variables, use globally forbidden/bypass flags, edit user tool configs, trust agent claims, or land/commit/merge without the user's explicit request.
Reason/result tables below use `| reason | code | meaning |`. `planrun` means `plan --run`: command exit 0/1 means completed/partial, 130 interruption, 2 preconditions, 70 internal error; nested task reasons retain task meanings. `do` route codes distinguish auth/quota/mixed access. Parser errors exit 2 without a structured reason. Map/plan signals return 128 + signal; task/plan-run interruption uses 130; do cancellation retains 1. README documents the full grammar, limits, evidence schemas and general exits.
<!-- plan-violation-reasons -->
| Violation | Reason |
| --- | --- |
| `empty_goal`, `empty_allow`, `empty_done` | Cleaned goal, allowed paths or done criteria are empty. |
| `duplicate_id`, `unknown_dependency`, `cycle` | Task IDs or dependency graph are invalid. |
| `size_l`, `too_many_tasks`, `no_tasks` | Tasks must have size S/M and number 1–12. |
| `parallel_overlap`, `invalid_glob` | Unordered parallel scopes overlap, or a glob is invalid/exceeds limits. |
| `no_test_path` | A resolved standard/risky task allows non-documentation paths but no L0 test path. |
<!-- /plan-violation-reasons -->
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
<!-- shared-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `acceptance_failed` | 1 | do: Creating the frozen acceptance commit failed. |
| `accepted` | 0 | task: The CLI accepted the frozen candidate. |
| `answer_invalid` | 2 | answer: The answer batch does not match the current form. |
| `audit_invalid` | 0 | task: Risky lane: the auditor reply was invalid twice (strict `orcexe.audit.v1`); not landable. |
| `auditor_unavailable` | 0 | task: Risky lane: no eligible cross-family auditor (run-time family), it failed, or its call budget is spent; no ref, no landing command. |
| `auditor_wrote` | 1 | task: Risky lane: the read-only auditor changed its own throwaway copy. |
| `budget` | 1 | task: `plan --run` budget (`--max-calls`/`--max-minutes`) refused the next model call; resumable with `--resume`. |
| `cancelled` | 130 | task: Task execution was interrupted. |
| `code_unchanged` | 1 | do: The edit changes executable code. |
| `completed` | 0 | planrun: Every task in the plan was accepted. |
| `contract_tampered` | 1 | task: The frozen contract changed after dispatch. |
| `contract_unapproved` | 2 | task: Risky lane: the contract proposal awaits the user's `task --approve-contract` (or the hash is not the current proposal's, `detail: proposal_changed`). |
| `controls` | 1 | do: Git controls could not be verified. |
| `dependency_failed` | 1 | planrun: A dependency was not accepted; this task was blocked. |
| `dependency_pending` | 2 | task: A dependency has not been accepted. |
| `diff_check` | 1 | do: Whitespace or conflict checks failed. |
| `diff_collect` | 1 | do: The complete change set could not be observed. |
| `exhausted` | 1 | task: All three attempts were consumed. |
| `form_tampered` | 2 | answer: The saved form content differs from its hash. |
| `gate_accepted` | 0 | do: All fast gates passed with isolation. |
| `gate_passed` | 0 | task: Mechanical gates passed; full acceptance is unverified. |
| `git_controls_changed` | 1 | task: Git control state changed during execution. |
| `git_identity_missing` | 1 | task, do: Configure Git author and committer identity. |
| `gitlink_tree` | 2 | map: The tree contains unsupported submodules. |
| `head_moved` | 2 | map: HEAD changed while observations were being collected. |
| `internal_error` | 70 | map, questions, task, planrun, do: Internal computation failed; inspect the reported step. |
| `interrupted` | 130 | planrun: Plan execution was interrupted; inspect before resuming. |
| `isolation_unavailable` | 0 | task: No usable OS sandbox; acceptance is withheld. |
| `isolation_unavailable` | 1 | do: No usable OS sandbox; acceptance is withheld. |
| `l1_basis_insufficient` | 0 | task: Risky lane: only a same-family or Cursor read-only L1 was available; `cross_family` is required. |
| `l1_invalid` | 0 | task: The evaluator rubric was invalid. |
| `l1_unavailable` | 0 | task: An independent evaluator could not complete. |
| `lane_not_supported` | 1 | planrun: This entry point cannot execute the selected lane. |
| `lane_not_supported` | 2 | task: This entry point cannot execute the selected lane. |
| `lane_risky` | 7 | do: Risky execution is not supported. |
| `lane_standard` | 7 | do: Use an approved standard-lane plan. |
| `ledger_corrupt` | 2 | task, planrun, status, do: The ledger has an invalid line (often a torn tail); run `orcexe ledger repair`. |
| `ledger_head_mismatch` | 2 | task, planrun, status, do: The ledger tail differs from its anchored head ref; run `orcexe ledger verify`, then `orcexe ledger repair`. |
| `main_repository_changed` | 1 | task, do: The executor changed the main repository. |
| `manager_output_invalid` | 2 | map: Manager output failed the closed schema or bounds. |
| `manager_tool_not_read_only` | 2 | map: Only Claude with the manager profile is allowed. |
| `manager_unavailable` | 2 | map: The manager could not complete. |
| `manager_unavailable: auth` | 2 | map: Manager failed: auth. |
| `manager_unavailable: failed` | 2 | map: Manager failed: failed. |
| `manager_unavailable: no_route` | 2 | map: Manager failed: no_route. |
| `manager_unavailable: quota` | 2 | map: Manager failed: quota. |
| `manager_unavailable: stalled` | 2 | map: Manager failed: stalled. |
| `manager_unavailable: timeout` | 2 | map: Manager failed: timeout. |
| `map_missing` | 2 | questions: Generate a project map first. |
| `map_stale` | 2 | questions: Refresh the map at the current HEAD. |
| `no_route` | 2 | task, planrun: The task has no eligible saved executor route. |
| `not_a_repository` | 2 | map, questions, task, planrun, status: The path is not a supported Git repository. |
| `observation_unavailable` | 1 | task: Required safety evidence could not be observed. |
| `orcexe_invalid` | 2 | map, questions, task, planrun, status: `.orcexe` at the repository root is a symlink or not a directory. |
| `output_symlink` | 2 | map, questions, task: An output path is an unexpected symlink. |
| `outside_allow` | 1 | task: A changed path is outside the frozen allowed scope. |
| `partial` | 1 | planrun: At least one task was not accepted. |
| `pipes_held` | 1 | task, do: An escaped process may still hold the executor pipes. |
| `plan_mismatch` | 2 | task, planrun: The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | task, planrun, status: No saved plan exists. |
| `plan_not_approved` | 2 | task, planrun: Approve this plan before executing it; after `approval.revoked` (`detail: approval_revoked`) approve it again. |
| `plan_stale` | 2 | task, planrun: The plan was made for another HEAD, its classifier or runnable lanes changed (`detail: classifier_changed`/`lanes_changed`), or it was approved before partial approval. |
| `plan_tampered` | 2 | task, planrun, status: The plan hash, approval lists, reasons, decision record or task shape is invalid. |
| `project_checks` | 1 | do: Project checks failed or were not observed. |
| `questions_invalid` | 2 | questions: The map, goal or question data is invalid. |
| `report_failed` | 70 | do: Writing the final report failed. |
| `risk_path_touched` | 1 | task: A changed path matches a protected risk area. |
| `route_forced_unavailable` | 1 | do: No eligible route (forced unavailable); mixed or other access failures. |
| `route_forced_unavailable` | 4 | do: No eligible route (forced unavailable); all candidates lack authentication. |
| `route_forced_unavailable` | 5 | do: No eligible route (forced unavailable); all candidates exhausted quota. |
| `route_no_eligible_candidate` | 1 | do: No eligible route (no eligible candidate); mixed or other access failures. |
| `route_no_eligible_candidate` | 4 | do: No eligible route (no eligible candidate); all candidates lack authentication. |
| `route_no_eligible_candidate` | 5 | do: No eligible route (no eligible candidate); all candidates exhausted quota. |
| `route_no_prior` | 1 | do: No eligible route (no prior); mixed or other access failures. |
| `run_interrupted` | 2 | task, planrun: Use --resume to continue an unfinished plan. |
| `run_status` | 1 | do: Executor runtime failed or completion could not be verified. |
| `run_status` | 4 | do: Executor runtime failed or completion could not be verified. |
| `run_status` | 5 | do: Executor runtime failed or completion could not be verified. |
| `run_status` | 6 | do: Executor runtime failed or completion could not be verified. |
| `same_failure` | 1 | task: Risky lane: the same failure class twice; task is `diagnosing`, no third attempt, not landable. |
| `scope` | 1 | do: Changes exceeded the allowed target. |
| `secrets` | 1 | do: Secret scanning failed. |
| `signal_SIGHUP` | 1 | do: Cancelled by SIGHUP; do retains its failure exit code. |
| `signal_SIGINT` | 1 | do: Cancelled by SIGINT; do retains its failure exit code. |
| `signal_SIGTERM` | 1 | do: Cancelled by SIGTERM; do retains its failure exit code. |
| `size` | 1 | do: Changes exceeded the fast-lane budget. |
| `state_corrupt` | 2 | questions, answer, task, planrun, status: Saved state or ledger is unreadable or inconsistent. |
| `state_dir_in_repository` | 2 | map: The resolved `ORCEXE_STATE_DIR` is inside the repository or its main worktree. |
| `state_tampered` | 1 | task, do: Project code, a run or a leftover process changed CLI control state (see `orcexe ledger repair` recovery), or a plan decision has no ledger anchor (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`; propose again). |
| `state_tampered` | 2 | planrun, status: Project code, a run or a leftover process changed CLI control state (see `orcexe ledger repair` recovery), or a plan decision has no ledger anchor (`detail: decision_unanchored`/`open_unanchored`/`decision_superseded`; propose again). |
| `task_deferred` | 2 | task: The task is deferred by the latest plan decision; open it with `plan --approve PLAN_ID --open`. |
| `task_running` | 1 | task: Another dispatch holds the repository lock. |
| `task_running` | 2 | planrun: Another dispatch holds the repository lock. |
| `task_unknown` | 2 | task: The task ID is absent from the approved plan. |
| `test_floor` | 1 | do: Tests were removed or skip markers were added. |
| `write_failed` | 2 | map, questions: Publishing private state failed. |
<!-- /shared-exit-reasons -->
