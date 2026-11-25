"""One attempt in the fast lane; only gate acceptance permits landing advice.

BRIEF is the fixed executor template. Task text is deliberately verbatim there,
never in reports except for a redacted 200-character preview. Runs (including
both brief copies) are private, git-excluded, under a 0700 runs directory.
Dry runs probe access and route, but take no lock and write no project state,
ledger, brief or worktree. Access probes are model-free.
"""

from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import secrets
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import traceback

from orcexe.adapters.base import safe_write_text

from orcexe import classify, exitcodes, fsio, gate_lite, gitutil, isolation, ledger, redact, route, runguard, trust, worktree
from orcexe.adapters.base import RunRequest, validate_run_result
from orcexe.adapters.codex import CodexAdapter
from orcexe.adapters.claude_code import ClaudeCodeAdapter
from orcexe.adapters.cursor import CursorAdapter


BRIEF = """# Task
{task}

# Allowed file
{target}

# Rules
- Edit only the single allowed file above.
- For source files, change only comments/docstrings.
- Do not create, delete, rename or move files.
- Do not run git commands that change history (commit is allowed but not needed).
- Do not install anything or use the network.
- End the final message with ORCEXE_EXIT: DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT
  (choose exactly one of these four values).
"""


def _utc():
    return datetime.now(timezone.utc)


class _Cancelled(BaseException):
    pass


class _DoFailure(RuntimeError):
    pass


def _validate_task_text(task):
    if not isinstance(task, str):
        raise ValueError("invalid task")
    try:
        task.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("surrogates not allowed") from error


def _repo_usage(message):
    return _report(dict(verdict="blocked", exit_code=exitcodes.USAGE, message=message))


def _orcexe_usage(error):
    return _report(dict(verdict="blocked", exit_code=exitcodes.USAGE, message=str(error)))


def _lock_data(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


# An empty or unparseable lock this old cannot be a writer between O_EXCL and fsync.
JUNK_LOCK_GRACE_S = 10


def _junk(path, info):
    """An empty or unparseable lock (no JSON object at all), older than the grace period.

    A parseable object is never junk, even without a valid pid (``{}``, ``{"pid": null}``):
    it may be a live owner's lock someone rewrote, so it blocks and names the manual step.
    """
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return False
    except (OSError, UnicodeError):
        return False
    except ValueError:
        pass
    return time.time() - info.st_mtime > JUNK_LOCK_GRACE_S


def _junk_candidate(path):
    """Empty/unparseable content (reclaimed after the grace period, so no manual step)."""
    try:
        return not isinstance(json.loads(path.read_text(encoding="utf-8")), dict)
    except (OSError, UnicodeError):
        return False
    except ValueError:
        return True


def _stale(path):
    try:
        info = path.stat()
        if _junk(path, info):
            return info
        data = _lock_data(path)
        pid = data.get("pid")
        # Legacy locks lacked host/task. Never reclaim a remote or live PID.
        if type(pid) is not int or pid <= 0 or data.get("host", platform.node()) != platform.node():
            return None
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return info
        except PermissionError:
            pass
    except OSError:
        pass
    return None


def _acquire(path, task_id, on_abandoned):
    path.parent.mkdir(mode=0o700, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            stale = _stale(path)
            if stale is None:
                pid = _lock_data(path).get("pid")
                if path.exists() and (type(pid) is not int or pid <= 0) and not _junk_candidate(path):
                    print("Warning: {} names no valid owner pid; if no orcexe command is running, "
                          "remove it by hand.".format(path), file=sys.stderr)
                return None
            reclaim = path.with_suffix(".reclaim")
            try:
                guard = os.open(reclaim, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                return None
            try:
                current = _stale(path)
                if current is not None and (current.st_dev, current.st_ino) == (stale.st_dev, stale.st_ino):
                    if _junk(path, current):
                        print("Warning: removed an empty or unreadable lock older than {}s: {}".format(
                            JUNK_LOCK_GRACE_S, path), file=sys.stderr)
                    on_abandoned(_lock_data(path))
                    path.unlink()
            finally:
                os.close(guard)
                reclaim.unlink()
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(pid=os.getpid(), created_utc=_utc().isoformat(),
                               task_id=task_id, host=platform.node()), handle)
                handle.flush()
                os.fsync(handle.fileno())
            return path.stat()
        except BaseException:
            path.unlink()
            raise
    return None


def _git(main, *args, index=None, identity=None, allow_failure=False):
    """Match gate_lite's hardened Git setup; never inherit Git overrides."""
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT")
           if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE=os.devnull,
               GIT_ALLOW_PROTOCOL="", LC_ALL="C", LANG="C")
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    if identity is not None:
        env.update(identity)
    argv = ["git", "--no-replace-objects", "--no-pager", "--literal-pathspecs",
            "-c", "core.fsmonitor=false", "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.attributesFile=" + os.devnull, "-c", "protocol.allow=never"]
    result = subprocess.run(argv + list(args), cwd=str(main), env=env,
                            stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
    if (result.returncode and not allow_failure or
            len(result.stdout) > 16 * 1024 * 1024 or len(result.stderr) > 16 * 1024 * 1024):
        raise _DoFailure("Incomplete Git observation: " + args[0])
    return result.returncode, result.stdout


def _config_snapshot(main):
    """Bind all project config bytes, including future config, excluding run state."""
    root = main / ".orcexe"
    excluded = {"runs", "do.lock", "do.reclaim", "ledger.jsonl", "trust-ledger.json", "env.json"}
    result = {}
    if root.is_symlink():
        raise _DoFailure("Unsafe main configuration directory")
    for folder, dirs, files in os.walk(root, followlinks=False):
        if Path(folder) == root:
            dirs[:] = [d for d in dirs if d not in excluded]
            files = [f for f in files if f not in excluded]
        for name in files + dirs:
            path = Path(folder) / name
            result[path.relative_to(root).as_posix()] = gate_lite._control_hash(path)
    return result


def _main_snapshot(main, task_id):
    code, symbolic = _git(main, "symbolic-ref", "-q", "HEAD", allow_failure=True)
    if code not in (0, 1):
        raise _DoFailure("Cannot observe main HEAD")
    _, oid = _git(main, "rev-parse", "--verify", "HEAD")
    _, refs = _git(main, "for-each-ref", "--format=%(refname) %(objectname) %(symref)")
    prefix = ("refs/heads/orcexe/do/" + task_id.lower() + "-").encode()
    # The ledger head ref moves with every CLI append; the run guard observes it instead.
    refs = tuple(line for line in refs.splitlines() if not line.split(b" ", 1)[0].startswith(prefix)
                 and line.split(b" ", 1)[0] != ledger.HEAD_REF.encode())
    _, status = _git(main, "status", "--porcelain=v1", "-z", "--untracked-files=all", "--ignored=no")
    if status and not status.endswith(b"\0"):
        raise _DoFailure("Incomplete main status")
    # Git uses literal pathspecs, so filter NUL records instead of relying on
    # pathspec magic. Keep a rename if either side is outside .orcexe/.
    records, kept = iter(status.split(b"\0")[:-1]), []
    for record in records:
        if len(record) < 4 or record[2:3] != b" ":
            raise _DoFailure("Invalid main status")
        names, group = [record[3:]], [record]
        if b"R" in record[:2] or b"C" in record[:2]:
            source = next(records, None)
            if source is None:
                raise _DoFailure("Incomplete main rename status")
            names.append(source)
            group.append(source)
        if any(n != b".orcexe" and not n.startswith(b".orcexe/") for n in names):
            kept.extend(group)
    return symbolic, oid, refs, tuple(kept), _config_snapshot(main)


def _resolve_identity(main):
    """Read user-owned config before dispatch; never infer a missing identity.

    Git precedence per field: GIT_AUTHOR_*/GIT_COMMITTER_* environment,
    author.*/committer.* config, then user.* config (local/global/system).
    Only read-only config queries may use this minimal user environment.
    """
    allowed = {"PATH", "HOME", "USER", "LOGNAME", "LANG", "XDG_CONFIG_HOME",
               "GIT_CONFIG_GLOBAL", "GIT_CONFIG_NOSYSTEM", "GIT_AUTHOR_NAME",
               "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"}
    env = {key: value for key, value in os.environ.items()
           if key in allowed or key.startswith("LC_")}

    def config(key):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=" + os.devnull, "config", "--get", key],
            cwd=str(main), env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=10)
        if result.returncode == 1:
            return None
        if result.returncode or len(result.stdout) > 16 * 1024 * 1024:
            raise _DoFailure("Cannot resolve Git identity: " + key)
        return result.stdout.decode("utf-8").rstrip("\n")

    user = {field: config("user." + field) for field in ("name", "email")}
    identity = {}
    for role in ("author", "committer"):
        for field in ("name", "email"):
            key = "GIT_" + role.upper() + "_" + field.upper()
            if key in env:
                value = env[key]
            else:
                value = config(role + "." + field)
                if value is None:
                    value = user[field]
            if value is None or not value.strip() or any(c in value for c in "\n\r\0"):
                return None
            identity[key] = value
    return identity


def _accepted_commit(main, wt, gate, task_id, preview, target, identity, *, message=None):
    """Compose solely from the base tree and the gate's immutable raw blob."""
    if message is None:
        message = "orcexe do " + task_id + ": " + preview
    gate_lite.validate_gate_result(gate)
    diff = gate["diff"]
    if diff["base_commit"] != wt.base_commit or diff["target_path"] != redact.redact_text(target):
        raise _DoFailure("Gate target/base mismatch")
    base, blob = wt.base_commit, diff["target_blob"]
    _, entry = _git(main, "ls-tree", "-z", base, "--", target)
    header, name = entry.rstrip(b"\0").split(b"\t", 1)
    mode, kind, _ = header.decode("ascii").split()
    if name != os.fsencode(target) or mode not in ("100644", "100755") or kind != "blob":
        raise _DoFailure("Invalid accepted target in base")
    with tempfile.TemporaryDirectory(prefix="orcexe-accepted-") as scratch:
        index = Path(scratch) / "index"
        _git(main, "read-tree", base, index=index)
        _git(main, "update-index", "--cacheinfo", mode + "," + blob + "," + target, index=index)
        _, tree = _git(main, "write-tree", index=index)
        _, commit = _git(main, "commit-tree", tree.decode().strip(), "-p", base,
                         "-m", message, identity=identity)
    commit = commit.decode().strip()
    _, names = _git(main, "diff-tree", "--no-commit-id", "--name-only", "--no-renames", "-r", "-z", base, commit)
    _, actual = _git(main, "ls-tree", "-z", commit, "--", target)
    if names != os.fsencode(target) + b"\0" or actual != mode.encode() + b" blob " + blob.encode() + b"\t" + name + b"\0":
        raise _DoFailure("Frozen acceptance tree mismatch")
    ref = "refs/orcexe/accepted/" + task_id
    _git(main, "update-ref", ref, commit, "0" * len(commit))
    return ref


def _exclude(main):
    _, common = gitutil._git(main, "rev-parse", "--git-common-dir")
    path = (main / common).resolve() / "info" / "exclude"
    previous = path.read_text(encoding="utf-8") if path.exists() else ""
    if "/.orcexe/" not in previous.splitlines():
        safe_write_text(path, previous + ("\n" if previous and not previous.endswith("\n") else "")
                               + "/.orcexe/\n")


def _route_exit(decision):
    reasons = [skip["reason"] for skip in decision["skipped"]]
    if reasons and all(reason in ("auth_missing", "auth_unknown") for reason in reasons):
        return exitcodes.AUTH
    if reasons and all(reason == "quota_exhausted" for reason in reasons):
        return exitcodes.QUOTA
    return exitcodes.FAIL


def _report(summary, directory=None):
    clean = redact.redact_obj(summary)
    lines = ["Verdict: " + clean["verdict"], clean.get("message", "")]
    if clean.get("abandoned_warning"):
        lines.append(clean["abandoned_warning"])
    if clean.get("failed_check"):
        lines.append("Failed check: {} — {}".format(clean["failed_check"], clean["detail"]))
    lines.extend("Warning: " + warning for warning in clean.get("warnings", []))
    if "changed_files" in clean:
        lines.append("Changed files: " + (", ".join(clean["changed_files"]) or "none"))
    for key, label in (("worktree", "Worktree"), ("branch", "Branch")):
        if key in clean:
            lines.append(label + ": " + clean[key])
    if "self_report" in clean:
        lines.append("agent says: " + json.dumps(clean["self_report"], ensure_ascii=False))
    if clean.get("land_commands"):
        lines.extend(["Human landing commands (review the frozen accepted commit):"]
                     + clean["land_commands"])
    elif clean["verdict"] != "planned":
        if clean.get("worktree"):
            followup = "Inspect status.md, gate.json, result.json and gate/ in {}; the worktree is kept.".format(directory)
        else:
            followup = "No executor was dispatched."
        lines.append("Nothing was landed. " + followup)
    clean["message"] = redact.redact_text("\n".join(line for line in lines if line) + "\n")
    if directory:
        safe_write_text(directory / "status.md", clean["message"], mode=0o600)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
    return clean


@ledger.in_session
def run_do(task, repo=None, *, dry_run=False, timeout=1800, model_constraint=None):
    """Return a redacted summary with exit_code; never prompt, print or land.

    model_constraint is a route constraint mapping (the CLI accepts JSON using
    route's existing tools/deny_models/force/exclude_families vocabulary).
    """
    log = directory = lock_info = lock_path = None
    task_id = adapter = run_id = None
    received = None
    handlers = {}

    def on_signal(signum, frame):
        nonlocal received
        if received is not None:
            return
        received = signum
        if adapter is not None and run_id is not None and adapter.cancel(run_id):
            return
        raise _Cancelled()

    state = "draft"
    summary = dict(verdict="failed", exit_code=exitcodes.INTERNAL)
    def internal_error(error):
        if log:
            try:
                log.append("task.state", task_id, {"from": state, "to": "failed", "reason": "internal_error"})
            except Exception:
                pass
        if directory:
            try:
                safe_write_text(directory / "internal-error.txt",
                                       redact.redact_text(traceback.format_exc()), mode=0o600)
            except Exception:
                pass
        summary.pop("land_commands", None)
        summary.update(verdict="failed", exit_code=exitcodes.INTERNAL,
                       message="Internal error: " + redact.redact_text(str(error))[:200])
        try:
            return _report(summary, directory)
        except Exception:
            return _report(summary)

    try:
        if os.name == "posix" and threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handlers[sig] = signal.signal(sig, on_signal)
        try:
            route._constraints(model_constraint)
        except (ValueError, TypeError) as error:
            return _report(dict(verdict="blocked", exit_code=exitcodes.USAGE,
                                message="Invalid --model-constraint: " + str(error)))
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be finite and positive")
        try:
            _validate_task_text(task)
        except ValueError as error:
            return _report(dict(verdict="blocked", exit_code=exitcodes.USAGE, message=str(error)))
        explicit_repo = repo is not None
        try:
            repo_path = gitutil.resolve_repo_path(repo) if explicit_repo else Path.cwd().resolve()
            if not gitutil.is_repo(repo_path):
                # An existing directory that is not a repository is everyday work
                # (unsupported until v0.4); only a missing --repo path is usage.
                return _report(dict(verdict="blocked", mode="everyday", exit_code=exitcodes.UNSUPPORTED,
                    message="v0.1 only handles code repositories; everyday tasks arrive in v0.4"))
            main = Path(gitutil.main_worktree(repo_path))
        except gitutil.GitError:
            return _repo_usage("The path is not a supported Git repository.")
        try:
            fsio.require_main_orcexe(main)
        except fsio.OrcexeDirError as error:
            return _orcexe_usage(error)
        task_id = "do-{}-{}".format(_utc().strftime("%Y%m%dT%H%M%S"), secrets.token_hex(2))
        run_id = task_id + "-r1"
        if not dry_run:
            def abandoned(data):
                stored = data.get("task_id")
                if not isinstance(stored, str):
                    return
                old_log = ledger.Ledger(ledger.default_path(main), clock=_utc)
                try:
                    old_log.verify()
                    old_log.append("task.state", stored, {"from": "running", "to": "failed", "reason": "abandoned"})
                except (ValueError, ledger.LedgerError):
                    pass
                # Use the owned dispatch record instead of deriving a path from lock text.
                try:
                    dispatches = list(old_log.records(task=stored, event="task.dispatched"))
                    branch = dispatches[-1]["data"]["branch"] if dispatches else None
                    paths = [w.path for w in worktree.list_managed(main) if w.branch == branch]
                except Exception:
                    paths = []
                summary["abandoned_warning"] = ("Abandoned task " + stored + "; kill -9 cannot stop its executor tree. "
                    "Inspect worktree: " + (", ".join(paths) or str(main.parent / (main.name + "-wt")) ))
            lock_path = main / ".orcexe" / "do.lock"
            lock_info = _acquire(lock_path, task_id, abandoned)
            if lock_info is None:
                return _report(dict(verdict="blocked", exit_code=exitcodes.FAIL,
                                    message="another orcexe do is running (pid {}, task {})".format(
                                        _lock_data(lock_path).get("pid", "unknown"),
                                        _lock_data(lock_path).get("task_id", "unknown"))))
            _exclude(main)
            # T55: whole chain plus external head before this command decides anything.
            try:
                ledger.Ledger(ledger.default_path(main), clock=_utc).verify_head()
            except (OSError, ledger.LedgerError) as error:
                if isinstance(error, ledger.HeadMismatch):
                    summary.update(verdict="blocked", exit_code=exitcodes.USAGE, reason="ledger_head_mismatch",
                                   message="not_seen: ledger_head_mismatch\n" + ledger.REPAIR_HINT)
                else:
                    summary.update(verdict="blocked", exit_code=exitcodes.USAGE, reason="ledger_corrupt",
                                   message="not_seen: ledger_corrupt\n" + ledger.REPAIR_HINT)
                return _report(summary)
        if not dry_run:
            directory = main / ".orcexe" / "runs" / run_id
        classification = classify.classify_repo(str(main), task)
        preview = redact.redact_text(task)[:200]
        summary.update(task_id=task_id, run_id=run_id, mode=classification.mode,
                       lane=classification.lane, signals=[s.id for s in classification.signals],
                       task_preview=preview)
        if not dry_run:
            log = ledger.Ledger(ledger.default_path(main), clock=_utc)
            log.append("task.classified", task_id, dict(mode=classification.mode, lane=classification.lane,
                signals=summary["signals"], targets_count=len(classification.targets),
                task_sha256=hashlib.sha256(task.encode("utf-8")).hexdigest(), task_preview=preview))

        def blocked(message, reason, code):
            if log:
                log.append("task.state", task_id, {"from": "draft", "to": "blocked", "reason": reason})
            summary.update(verdict="blocked", exit_code=code, message=message)
            return _report(summary, directory)

        if classification.lane != "fast":
            return blocked("Lane: {}; signals: {}\nthis task needs the standard/risky lane "
                "(v0.2+: `orcexe plan`)".format(classification.lane, ", ".join(summary["signals"])),
                "lane_" + classification.lane, exitcodes.UNSUPPORTED)
        registry = {"codex": CodexAdapter, "claude-code": ClaudeCodeAdapter, "cursor": CursorAdapter}
        access = route.gather_access([factory() for factory in registry.values()])
        decision = route.route("executor", "fast", access, route.load_roles(), constraints=model_constraint)
        summary["route"] = decision
        if decision["tool"] is None:
            return blocked("Route: {}; skipped: {}".format(decision["reason"],
                json.dumps(decision["skipped"])), "route_" + decision["reason"], _route_exit(decision))
        adapter = registry[decision["tool"]]()
        identity = _resolve_identity(main)
        if identity is None:
            return blocked("configure git user.name and user.email "
                           "(git config --global user.name …; git config --global user.email …)",
                           "git_identity_missing", exitcodes.FAIL)
        if dry_run:
            summary.update(verdict="planned", exit_code=exitcodes.OK, message="Dry run; route: "
                           + json.dumps(decision) + "; allowed file: " + classification.targets[0])
            return _report(summary)
        wt = worktree.create(main, worktree.Spec(plan="do", task=task_id.lower(), tool=decision["tool"]))
        try:
            controls = gate_lite.snapshot_controls(main, wt)
            # Freeze every classifier/gate input before the executor gets control.
            classification = classify.classify_repo(str(main), task)
            commands = gate_lite.detect_commands(main, wt.base_commit)
            main_before = _main_snapshot(main, task_id)
        finally:
            summary.update(worktree=wt.path, branch=wt.branch)
        safe_write_text(directory / "brief-src.md", BRIEF.format(task=task,
                               target=classification.targets[0]), mode=0o600)
        os.chmod(directory.parent, 0o700)
        os.chmod(directory, 0o700)
        log.append("task.dispatched", task_id, dict(tool=decision["tool"], model=decision["model"],
            effort=decision["effort"], route_basis="; ".join(decision["basis"]), branch=wt.branch, attempt=1))
        state = "running"
        log.append("orchestrator.usage", task_id, dict(input=None, output=None, source="not_seen"))
        trust_before = None
        trust_config = trust.config_path() if decision["tool"] == "codex" else None
        if trust_config is not None:
            try:
                trust_before = trust.headers(trust_config.parent)
            except Exception as error:
                summary.setdefault("warnings", []).append("trust_snapshot_failed:" + type(error).__name__)
        request = RunRequest(run_id=run_id, task=task_id, tool=decision["tool"],
            model=decision["model"], effort=decision["effort"], profile="write", workdir=wt.path,
            brief_path=str(directory / "brief-src.md"), run_dir=str(directory), timeout_s=timeout)
        try:
            # P2-2: any ledger/head/run-directory change during the executor is terminal.
            result = runguard.Guard(main, directory.parent, log).run(lambda: adapter.run(request), directory)
        finally:
            if trust_before is not None:
                try:
                    trust_after = trust.headers(trust_config.parent)
                    trust.record(main / ".orcexe" / "trust-ledger.json", trust_before, trust_after,
                                 run_id, trust_config)
                except Exception as error:
                    summary.setdefault("warnings", []).append("trust_ledger_record_failed:" + type(error).__name__)
        # Compare controls first: never run Git under changed executor controls.
        try:
            unchanged = (gate_lite.snapshot_controls(main, wt) == controls
                         and _main_snapshot(main, task_id) == main_before)
        except Exception:
            unchanged = False
        if received is not None:
            raise _Cancelled()
        validate_run_result(result, run_dir=directory)
        log.append("attempt.ended", task_id, {key: result[key] for key in
                   ("run_id", "status", "exit_code", "duration_ms", "usage", "warnings")})
        if not unchanged:
            summary.update(verdict="failed", exit_code=exitcodes.FAIL,
                failed_check="main_repository_changed", detail="main HEAD, refs, status or Git controls changed",
                message="Main repository changed during executor run.", self_report=result["self_report"])
            report = _report(summary, directory)
            log.append("task.state", task_id, {"from": state, "to": "failed", "reason": "main_repository_changed"})
            state = "failed"
            return report
        if "pipes_held" in result["warnings"]:
            summary.update(verdict="unverified", exit_code=exitcodes.FAIL, failed_check="pipes_held",
                           detail="escaped executor processes may still be alive", message="Executor warning: pipes_held",
                           self_report=result["self_report"])
            report = _report(summary, directory)
            log.append("task.state", task_id, {"from": state, "to": "unverified", "reason": "pipes_held"})
            state = "unverified"
            return report
        gate = gate_lite.gate(repo_path=main, worktree=wt, classification=classification,
                             run_result=result, run_dir=directory, controls_before=controls, commands=commands)
        failed = next((c for c in gate["checks"] if c["status"] in ("fail", "not_seen")), None)
        verdict = gate["verdict"]
        isolated = isolation.evidence(gate)
        reason = 'state_tampered' if isolation.tampered(gate) else (
            'isolation_unavailable' if verdict == 'accepted' and isolated == 'none' else None)
        if reason:
            verdict = 'failed' if reason == 'state_tampered' else 'unverified'
            failed = dict(id=reason, detail=reason)
            summary['reason'] = reason
        try:
            log.append("gate.result", task_id, dict(run_id=run_id, verdict=verdict,
                failed_check=failed["id"] if failed else None,
                duration_ms=sum(c["duration_ms"] for c in gate["checks"]), isolation=isolated))
        except (OSError, ValueError, ledger.LedgerError):
            if reason != 'state_tampered':
                raise
        code = exitcodes.OK if verdict == "accepted" else {
            "quota": exitcodes.QUOTA, "auth": exitcodes.AUTH, "timeout": exitcodes.TIMEOUT,
            "stalled": exitcodes.TIMEOUT}.get(result["status"], exitcodes.FAIL)
        summary.update(verdict=verdict, exit_code=code, run_status=result["status"],
            failed_check=failed["id"] if failed else None, detail=failed["detail"] if failed else None,
            changed_files=[f["path"] for f in gate["diff"]["files"]], self_report=result["self_report"])
        if verdict == "accepted":
            ref = _accepted_commit(main, wt, gate, task_id, preview, classification.targets[0], identity)
            _, head = _git(main, "rev-parse", "--verify", "HEAD")
            command = ("merge --ff-only " if head.decode().strip() == wt.base_commit else "cherry-pick ") + shlex.quote(ref)
            summary["land_commands"] = ["git -C " + shlex.quote(str(main)) + " " + command]
        try:
            report = _report(summary, directory)
        except Exception:
            log.append("task.state", task_id, {"from": state, "to": "failed", "reason": "report_failed"})
            state = "failed"
            raise
        try:
            log.append("task.state", task_id, {"from": "running", "to": verdict,
                                              "reason": failed["id"] if failed else "gate_accepted"})
        except (OSError, ValueError, ledger.LedgerError):
            if reason != 'state_tampered':
                raise
        state = verdict
        if verdict == "escalated":
            log.append("lane.escalated", task_id, dict(from_lane="fast", to_lane="standard", reason=failed["id"]))
        return report
    except (isolation.StateTampered, ledger.ForeignRecord):
        summary.pop('land_commands', None)
        summary.update(verdict='failed', exit_code=exitcodes.FAIL, reason='state_tampered',
                       failed_check='state_tampered', detail='state_tampered')
        if log:
            try:
                log.append("task.state", task_id, {"from": state, "to": "failed", "reason": "state_tampered"})
            except Exception:
                pass  # A forged or moved ledger must not mask the terminal reason.
        try:
            return _report(summary, directory)
        except (OSError, ValueError):
            return _report(summary)
    except (KeyboardInterrupt, _Cancelled):
        received = received or signal.SIGINT
        reason = "signal_" + signal.Signals(received).name
        summary.pop("land_commands", None)
        summary.update(verdict="cancelled", exit_code=exitcodes.FAIL, signal_exit_code=128 + received,
                       message="Cancelled: " + reason)
        try:
            report = _report(summary, directory)
        except Exception:
            report = _report(summary)
        if log:
            log.append("task.state", task_id, {"from": state, "to": "cancelled", "reason": reason})
        return report
    except _DoFailure as error:
        summary.pop("land_commands", None)
        summary.update(verdict="failed", exit_code=exitcodes.FAIL, message=str(error))
        try:
            report = _report(summary, directory)
        except Exception as report_error:
            return internal_error(report_error)
        if log:
            log.append("task.state", task_id, {"from": state, "to": "failed", "reason": "acceptance_failed"})
        return report
    except Exception as error:
        return internal_error(error)
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if lock_info is not None:
            try:
                current = lock_path.stat()
                if (current.st_dev, current.st_ino) == (lock_info.st_dev, lock_info.st_ino):
                    lock_path.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                return internal_error(error)
