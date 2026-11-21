---
name: orcexe
description: Use when the user wants another AI coding tool (Codex, Claude Code, Cursor) to do a small, well-scoped change in this git repository and have it checked before landing, or asks to check whether orcexe/the AI CLIs are ready here ("orcexe doctor", "is codex set up", "run this with codex").
---
# orcexe
## Workflow

Call `python3 "${CLAUDE_PLUGIN_ROOT}/bin/orcexe" COMMAND` from the repository; use `--repo` where supported and `--json` for parsing. Run `doctor` before the first `do` or for setup; `--quick` skips quota/app-server probes. Use `probe TOOL detect|auth|models|quota` for details. Never install or authenticate a tool without the user's request.
Use `map` → `questions`/`answer` → `plan --goal TEXT` → human review → `plan --approve PLAN_ID` → `plan --run PLAN_ID` (or `task --plan PLAN_ID --task T1`) → `orcexe status` → review frozen refs and land manually. `--resume` continues interrupted plans without replenishing attempts. `do "TASK"` is the fast single-target comments/docs lane; standard work needs a plan, risky execution is unsupported. Agent completion claims never establish acceptance.
Map is incremental from `intake_sha`; missing/invalid maps, nonancestor HEAD, `--full` or over 40% changed files force a full rebuild. No-change maps skip models. Human JSON additions persist as `source: human`, deletions persist, restorations become proposals. Edited prose is kept with adjacent proposals and `human_edit_kept`. Current-state is always regenerated from CLI facts; retain all human blocks verbatim, including malformed tails, and save outside-block edits once to `current-state.previous.md`, reporting `current_state_previous_saved`.
`questions [--goal TEXT]` saves a form; `answer --form ID --choice QID=INDEX --text QID=TEXT` records validated batches (indexes start at zero). At most four answers per normalized goal; decided question IDs apply globally. Pending questions block planning. Plan P3 validates scope, dependency graph, sizes S/M, 1–12 tasks, shared bounded globs and test paths; plans require matching HEAD, canonical digest and approval before execution.
Standard tasks freeze `contract.lite.json` (scope, done criteria, base, three shared attempts) before dispatch. Candidate checks precede L0 test-count/skip/assertion checks and red-first tests on the base. An independent L1 evaluator judges each done criterion, preferring another family and excluding the executor model/Cursor. Failed gates/rubrics feed the next attempt; invalid/missing evaluation leaves `gate_passed`. Gates check the cumulative tree against the task base: a retry reporting `empty_diff` (work already done) is gated on that tree, while a first-attempt `empty_diff` or a never-changed tree still fails. Project-check failures in feedback name the check and detail (e.g. `unittest: timeout (600s)`) so the executor re-checks rather than rewrites. Scope/risk/control violations are terminal; contract widening is forbidden. Status reads ledger facts without calling models; task reasons do not change a successful status read's exit 0.
L0 and `slop` share test-check detection: `assert`, assertion calls (`assert`/`expect`/`should`/`verify` prefixes on any receiver), `fail`, exception contexts, `raise`/`throw`, XCTest and Swift `#expect`/`#require`. Hollow bodies still fail; slop retains constant-only/product-only precision signals. Dead exports ignore same-file uses and Python scripts (shebang/main guard, scripts/bin/tools, or unimported non-package files). Deno URL/specifier imports and deno.json/import_map.json mappings are declared; repository Python modules (including literal sys.path directories) are local. Python manifest/lock and Node package-lock/shrinkwrap declarations count; absent third-party imports remain findings. All scans are offline over bounded HEAD data.
Gate/L0/red-first steps get fresh temp HOME/scratch: Seatbelt on macOS, bwrap on Linux, network off, writes only to checkout/scratch and runner devices. Missing/denied sandbox means `isolation_unavailable`: task `gate_passed` (0), do `unverified` (1); no full acceptance. CLI control-state tampering is terminal `state_tampered`; investigate and restore trusted state before removing its marker. Claude manager/evaluator uses restricted safe mode; a Claude **executor** loads the user's own Claude hooks/plugins. Codex/Cursor read their own user config.
Main `.orcexe/` stores map/intake/reports/forms/decisions/plans, ledger, `current-state.md`, `status.md`, private contracts/attempt evidence in `runs/`, publication generations/backups/proposals, locks and archives. State defaults to `~/.cache/orcexe` (`env.json`, manager runs/runtime/gate scratch); relocate with absolute `ORCEXE_STATE_DIR` outside the repo. Sibling `<repo>-wt/` holds executor worktrees. Git adds `orcexe/…` branches, `refs/orcexe/accepted/…`, `refs/orcexe/plans/…`, worktree metadata/receipts and an `info/exclude` entry. Temporary copies/HOMEs are removed normally. orcexe itself writes nothing else in `~`; tool config effects and OS-owned runtime caches (such as macOS system Python caches) are separate.
Codex may append project trust in `${CODEX_HOME:-~/.codex}/config.toml`; Cursor creates project trust/runtime dirs and its five saved model keys are restored. `trust list` and `trust clean` preview recorded entries; `trust clean --apply --yes` needs the user's approval of candidates and writes a backup. Until v0.5 `clean`, stop runs, save changes/evidence, inspect `git worktree list`, remove exact worktrees with `git worktree remove`, then review/delete their branches/private refs. Remove chosen `.orcexe/` and resolved state files only after preserving approvals/resume history; state may be shared. Review receipts/exclude entries and empty sibling folders; inspect Cursor dirs separately. Never delete entire user tool configs. README lists all locations and manual commands.
Manual `run --tool TOOL --model MODEL --effort EFFORT --brief FILE --workdir LINKED_WORKTREE [--profile review|write]` has no gate and requires the user's chosen parameters. Write refuses main. Report observed status, requested/actual model, changed files and evidence. Never pass API keys/billing variables, use globally forbidden/bypass flags, edit user tool configs, trust agent claims, or land/commit/merge without the user's explicit request.
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

<!-- shared-exit-reasons -->
| Reason | Code | Meaning |
| --- | --- | --- |
| `acceptance_failed` | 1 | do: Creating the frozen acceptance commit failed. |
| `accepted` | 0 | task: The CLI accepted the frozen candidate. |
| `answer_invalid` | 2 | answer: The answer batch does not match the current form. |
| `cancelled` | 130 | task: Task execution was interrupted. |
| `code_unchanged` | 1 | do: The edit changes executable code. |
| `completed` | 0 | planrun: Every task in the plan was accepted. |
| `contract_tampered` | 1 | task: The frozen contract changed after dispatch. |
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
| `l1_invalid` | 0 | task: The evaluator rubric was invalid. |
| `l1_unavailable` | 0 | task: An independent evaluator could not complete. |
| `lane_not_supported` | 1 | planrun: This entry point cannot execute the selected lane. |
| `lane_not_supported` | 2 | task: This entry point cannot execute the selected lane. |
| `lane_risky` | 7 | do: Risky execution is not supported. |
| `lane_standard` | 7 | do: Use an approved standard-lane plan. |
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
| `output_symlink` | 2 | map, questions, task, planrun, status: An output path is an unexpected symlink. |
| `outside_allow` | 1 | task: A changed path is outside the frozen allowed scope. |
| `partial` | 1 | planrun: At least one task was not accepted. |
| `pipes_held` | 1 | task, do: An escaped process may still hold the executor pipes. |
| `plan_mismatch` | 2 | task, planrun: The supplied plan ID does not match saved state. |
| `plan_missing` | 2 | task, planrun, status: No saved plan exists. |
| `plan_not_approved` | 2 | task, planrun: Approve this plan before executing it. |
| `plan_stale` | 2 | task, planrun: The plan was made for another HEAD. |
| `plan_tampered` | 2 | task, planrun, status: The plan hash, approval or task shape is invalid. |
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
| `scope` | 1 | do: Changes exceeded the allowed target. |
| `secrets` | 1 | do: Secret scanning failed. |
| `signal_SIGHUP` | 1 | do: Cancelled by SIGHUP; do retains its failure exit code. |
| `signal_SIGINT` | 1 | do: Cancelled by SIGINT; do retains its failure exit code. |
| `signal_SIGTERM` | 1 | do: Cancelled by SIGTERM; do retains its failure exit code. |
| `size` | 1 | do: Changes exceeded the fast-lane budget. |
| `state_corrupt` | 2 | questions, answer, task, planrun, status: Saved state or ledger is unreadable or inconsistent. |
| `state_tampered` | 1 | task, do: Project code changed CLI control state; investigate before retrying. |
| `state_tampered` | 2 | planrun: Project code changed CLI control state; investigate before retrying. |
| `task_running` | 1 | task: Another dispatch holds the repository lock. |
| `task_running` | 2 | planrun: Another dispatch holds the repository lock. |
| `task_unknown` | 2 | task: The task ID is absent from the approved plan. |
| `test_floor` | 1 | do: Tests were removed or skip markers were added. |
| `write_failed` | 2 | map, questions: Publishing private state failed. |
<!-- /shared-exit-reasons -->
