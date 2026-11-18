"""Run one user-selected executor; observed results alone determine the exit."""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import signal
import threading

from orcexe.adapters.base import (safe_write_text, safe_write_json)

from orcexe import exitcodes, gitutil, redact, trust
from orcexe.adapters import (ClaudeCodeAdapter, CodexAdapter, CursorAdapter,
                             RunRequest, validate_run_result)


class _Cancelled(BaseException):
    pass


def result_exit_code(result):
    """Map observed run.v1 status to the manual-run CLI contract."""
    if "pipes_held" in result["warnings"]:
        return exitcodes.USAGE
    if result["status"] == "failed":
        return exitcodes.FAIL
    if result["status"] == "completed" and not (
            result["profile"] == "write" and "empty_diff" in result["warnings"]):
        return exitcodes.OK
    return exitcodes.USAGE


def _minimal_result(req, family, status, warnings, signum=None):
    """A valid receipt when no adapter receipt could be obtained."""
    directory = Path(req.run_dir)
    paths = {key: str(directory / name) for key, name in (
        ("final_text_path", "final.md"), ("events_path", "events.jsonl"),
        ("stderr_tail_path", "stderr.txt"))}
    for path in paths.values():
        if not Path(path).exists():
            safe_write_text(path, "", mode=0o600)
    return dict(schema="orcexe.run.v1", run_id=req.run_id, task=req.task,
        tool=req.tool, tool_version=None, family=family, model_requested=req.model,
        model_actual=None, effort=req.effort, profile=req.profile, context="fresh",
        status=status, exit_code=None, signal=signum, head_before=None, head_after=None,
        dirty_before=[], diff_files=[], untracked=[], self_report={"exit": None, "needs": []},
        usage={key: None for key in ("input", "cached_read", "cache_write", "output", "reasoning")},
        cost={"list_usd": None, "basis": "unknown"},
        quota_after={"windows": [], "source": "none"}, duration_ms=0,
        warnings=warnings, **paths)


def _human(result):
    lines = ["Status: " + result["status"],
             "Model requested/actual: {} / {}".format(
                 result["model_requested"], result["model_actual"] or "unknown"),
             "Duration: {:.3f}s".format(result["duration_ms"] / 1000),
             "Changed files: " + (", ".join(result["diff_files"]) or "none"),
             "Warnings: " + (", ".join(result["warnings"]) or "none"),
             "agent says: " + json.dumps(result["self_report"], ensure_ascii=False)]
    if "main_worktree_write_refused" in result["warnings"]:
        lines.append("Create a separate worktree with `git worktree add …` and use --workdir there.")
    directory = Path(result["final_text_path"]).parent
    lines.extend(str(directory / name) for name in ("result.json", "events.jsonl", "final.md"))
    return "\n".join(lines)


def run_command(args):
    """Execute the parsed run command, print redacted evidence, return 0/1/2."""
    try:
        workdir = Path(args.workdir).resolve()
        if not workdir.is_dir():
            raise ValueError("--workdir must be an existing directory")
        brief = Path(args.brief).resolve()
        if not brief.is_file():
            raise ValueError("--brief must be an existing readable file")
        with brief.open("rb") as handle:
            contents = handle.read(1024 * 1024 + 1)
        if len(contents) > 1024 * 1024:
            raise ValueError("--brief must be at most 1 MiB")
        try:
            contents = contents.decode("utf-8")
        except UnicodeError:
            raise ValueError("--brief must be UTF-8") from None
        if not gitutil.is_repo(workdir):
            raise ValueError("orcexe run needs a git repository in v0.1; non-code work arrives "
                             "with the everyday mode in v0.4")
        main = Path(gitutil.main_worktree(workdir))
        run_id = args.run_id if args.run_id is not None else "run-{}-{}".format(
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S"), secrets.token_hex(2))
        # Validate the id and all request fields before creating any state.
        req = RunRequest(run_id, run_id, args.tool, args.model, args.effort, args.profile,
                         str(workdir), str(brief), str(main / ".orcexe" / "runs" / run_id),
                         timeout_s=args.timeout_min * 60, stall_s=args.stall_min * 60)
        directory = Path(req.run_dir)
        for parent in (main / ".orcexe", directory.parent, directory):
            if parent.is_symlink():
                raise ValueError("Refusing symlink in run directory")
        # Use the same bounded, config-isolated Git observation as gitutil.
        _, common = gitutil._git(main, "rev-parse", "--git-common-dir")
        exclude = (main / common).resolve() / "info" / "exclude"
        if exclude.is_symlink():
            raise ValueError("Refusing symlink at git exclude")
        previous = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        if "/.orcexe/" not in previous.splitlines():
            safe_write_text(exclude, previous + ("\n" if previous and
                                   not previous.endswith("\n") else "") + "/.orcexe/\n")
        (main / ".orcexe").mkdir(mode=0o700, exist_ok=True)
        directory.parent.mkdir(mode=0o700, exist_ok=True)
        directory.mkdir(mode=0o700)  # A run id is never reused, even after interruption.
        os.chmod(directory, 0o700)
        snapshot = directory / "brief-source.md"
        safe_write_text(snapshot, contents, mode=0o600)
        req = RunRequest(run_id, run_id, args.tool, args.model, args.effort, args.profile,
                         str(workdir), str(snapshot), str(directory),
                         timeout_s=args.timeout_min * 60, stall_s=args.stall_min * 60)
        adapter = {"codex": CodexAdapter, "claude-code": ClaudeCodeAdapter,
                   "cursor": CursorAdapter}[args.tool]()
    except (OSError, ValueError, gitutil.GitError) as error:
        # Decode/OS diagnostics may contain brief bytes or other private values.
        message = str(error) if type(error) is ValueError else type(error).__name__
        print(redact.redact_text(json.dumps({"error": message}) if args.json else message))
        return exitcodes.USAGE

    received = None
    handlers = {}
    warnings = []
    result = None
    publishing = False

    def on_signal(signum, frame):
        nonlocal received
        if received is not None:
            return
        received = signum
        # Active adapters return only after proc has stopped and reaped the tree.
        if publishing:
            return
        if not adapter.cancel(run_id):
            raise _Cancelled()

    try:
        if threading.current_thread() is threading.main_thread():
            for name in ("SIGINT", "SIGTERM", "SIGHUP"):
                sig = getattr(signal, name, None)
                if sig is not None:
                    handlers[sig] = signal.signal(sig, on_signal)
        if req.profile == "write" and Path(gitutil.toplevel(workdir)) == main:
            result = _minimal_result(req, adapter.manifest.family, "blocked",
                                     ["main_worktree_write_refused"])
        else:
            before = None
            config = trust.config_path() if req.tool == "codex" and req.profile == "write" else None
            if config is not None:
                try:
                    before = trust.headers(config.parent)
                except (OSError, ValueError) as error:
                    warnings.append("trust_snapshot_failed:" + type(error).__name__)
            try:
                result = adapter.run(req)
            finally:
                if before is not None:
                    try:
                        trust.record(main / ".orcexe" / "trust-ledger.json", before,
                                     trust.headers(config.parent), run_id, config)
                    except (OSError, ValueError) as error:
                        warnings.append("trust_ledger_record_failed:" + type(error).__name__)
    except (_Cancelled, KeyboardInterrupt):
        received = received or signal.SIGINT
    except Exception as error:
        warnings.append("run_error:" + type(error).__name__)
    finally:
        # Handlers remain installed through publication; repeated signals cannot
        # interrupt cleanup or leave a partial receipt.
        publishing = True
        try:
            if result is None:
                result = _minimal_result(req, adapter.manifest.family,
                                         "cancelled" if received else "blocked", [], received)
            while True:
                publishing_signal = received
                if publishing_signal is not None:
                    result["status"] = "cancelled"
                    warnings.append("signal_" + signal.Signals(publishing_signal).name)
                result["warnings"] = list(dict.fromkeys(result["warnings"] + warnings))
                validate_run_result(result, run_dir=directory)
                safe_write_json(directory / "result.json", result, mode=0o600)
                if received == publishing_signal:
                    break
        finally:
            for sig, handler in handlers.items():
                signal.signal(sig, handler)

    output = json.dumps(result, ensure_ascii=False) if args.json else _human(result)
    print(redact.redact_text(output))
    return result_exit_code(result)
