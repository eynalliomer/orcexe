## 0.1.0 — 2026-11-18 (local tag; not published)

Walking skeleton: `orcexe do` runs one fast-lane task end to end with Codex, Claude Code or Cursor in an isolated worktree, gates it with the project's own checks and leaves landing to the human. Known gaps: CI has not run yet (no push), the review profile was not run against the real tools, native Windows is unverified.

- Add internal model-free `intake` discovery from bounded committed Git blobs, private U1 JSON, opaque instruction fingerprints, candidate commands and declaration-only security evidence; never fetch secret paths or execute project code.
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
