"""Command-line entry point for orcexe."""

import argparse
import json
import math
from pathlib import Path
import sys

from orcexe import exitcodes


def _timeout(value):
    try:
        result = float(value)
        if math.isfinite(result) and result > 0:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("timeout must be finite and positive")


def _trust_command(args):
    from orcexe import gitutil, trust

    def confirm(keys):
        if args.yes:
            return True
        if not sys.stdin.isatty():
            return False
        # Keep --json stdout a single JSON document, including on a TTY.
        print("Clean recorded Codex/Cursor trust entries:\n" + "\n".join(keys), file=sys.stderr)
        print("Continue? [y/N] ", end="", file=sys.stderr, flush=True)
        try:
            return input().strip().lower() == "y"
        except EOFError:
            return False

    try:
        main = Path(gitutil.main_worktree(args.repo or Path.cwd()))
        if (main / ".orcexe").is_symlink():
            raise trust.TrustError("Refusing symlink at main .orcexe")
        ledger_path = main / ".orcexe" / "trust-ledger.json"
        if args.trust_command == "list":
            result = {"entries": trust.list_entries(ledger_path, args.codex_home)}
            code = 0
            lines = [entry["state"] + " " + entry["key"] for entry in result["entries"]]
        else:
            result = trust.clean(ledger_path, args.codex_home, apply=args.apply, confirm=confirm)
            code = 1 if result["confirmed"] is False else 0
            lines = []
            for label in ("candidates", "kept_live", "skipped_edited", "skipped_unknown", "removed"):
                lines.extend(label + " " + key for key in result[label])
            if result["backup"]:
                lines.append("Backup: " + result["backup"])
            if code == 1:
                lines.append("Not confirmed; use --yes or confirm on a TTY.")
        print(json.dumps(result, ensure_ascii=False) if args.json else "\n".join(lines) or "No trust entries.")
        return code
    except (OSError, ValueError, gitutil.GitError) as error:
        # Never include config contents (including Unicode decode diagnostics).
        message = "Trust operation failed: " + (str(error) if isinstance(error, trust.TrustError)
                                                else type(error).__name__)
        print(json.dumps({"error": message}) if args.json else message)
        return 2


def main(argv=None):
    """Run the CLI and return its exit status."""
    parser = argparse.ArgumentParser(prog="orcexe", description="Orchestrate AI coding tools.")
    subcommands = parser.add_subparsers(dest="command")
    version_parser = subcommands.add_parser("version", help="Show the orcexe version")
    version_parser.add_argument("--json", action="store_true", help="Print version as JSON")
    map_parser = subcommands.add_parser("map", help="Build an evidence-checked project model")
    map_parser.add_argument("--repo", default=None)
    map_parser.add_argument("--json", action="store_true")
    map_parser.add_argument("--tool", choices=("claude-code", "codex", "cursor"))
    map_parser.add_argument("--model")
    map_parser.add_argument("--full", action="store_true", help="Rebuild the complete map")
    plan_parser = subcommands.add_parser("plan", help="Propose or approve a checked task plan")
    plan_parser.add_argument("--repo", default=None)
    plan_parser.add_argument("--json", action="store_true")
    plan_action = plan_parser.add_mutually_exclusive_group(required=True)
    plan_action.add_argument("--goal")
    plan_action.add_argument("--approve", metavar="PLAN_ID")
    plan_action.add_argument("--run", metavar="PLAN_ID")
    plan_parser.add_argument("--resume", action="store_true")
    status_parser = subcommands.add_parser("status", help="Read the latest plan and ledger status")
    status_parser.add_argument("--repo", default=None)
    status_parser.add_argument("--json", action="store_true")
    task_parser = subcommands.add_parser("task", help="Dispatch one task from an approved plan")
    task_parser.add_argument("--plan", required=True, metavar="PLAN_ID")
    task_parser.add_argument("--task", required=True, metavar="TASK_ID")
    task_parser.add_argument("--repo", default=None)
    task_parser.add_argument("--json", action="store_true")
    task_parser.add_argument("--timeout-min", type=_timeout, default=30)
    questions_parser = subcommands.add_parser("questions", help="Select up to four project decisions offline")
    questions_parser.add_argument("--repo", default=None)
    questions_parser.add_argument("--goal")
    questions_parser.add_argument("--json", action="store_true")
    answer_parser = subcommands.add_parser("answer", help="Record answers to the current form")
    answer_parser.add_argument("--repo", default=None)
    answer_parser.add_argument("--form", required=True)
    answer_parser.add_argument("--choice", action="append", default=[])
    answer_parser.add_argument("--text", action="append", default=[])
    answer_parser.add_argument("--json", action="store_true")
    intake_parser = subcommands.add_parser("intake", help="Inspect committed project metadata offline")
    intake_parser.add_argument("--repo", default=None)
    intake_parser.add_argument("--json", action="store_true")
    history_parser = subcommands.add_parser("history", help="Inspect committed history and direction sources")
    history_parser.add_argument("--repo", default=None)
    history_parser.add_argument("--json", action="store_true")
    history_parser.add_argument("--issues", action="store_true")
    slop_parser = subcommands.add_parser("slop", help="Scan committed source for quality signals offline")
    slop_parser.add_argument("--repo", default=None)
    slop_parser.add_argument("--json", action="store_true")
    doctor_parser = subcommands.add_parser("doctor", help="Check local readiness without model calls")
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.add_argument("--quick", action="store_true", help="Skip quota and app-server probes")
    doctor_parser.add_argument("--tool", choices=("codex", "claude-code", "cursor"))
    doctor_parser.add_argument("--no-cache", action="store_true")
    probe_parser = subcommands.add_parser("probe", help="Run one model-free adapter probe")
    probe_parser.add_argument("tool", choices=("codex", "claude-code", "cursor"))
    probe_parser.add_argument("operation", choices=("detect", "auth", "models", "quota"))
    probe_parser.add_argument("--json", action="store_true")
    do_parser = subcommands.add_parser("do", help="Run one fast-lane task in an isolated worktree")
    do_parser.add_argument("task")
    do_parser.add_argument("--repo", default=None, help="Repository (default: current directory)")
    do_parser.add_argument("--dry-run", action="store_true")
    do_parser.add_argument("--json", action="store_true")
    do_parser.add_argument("--timeout", type=_timeout, default=1800)
    do_parser.add_argument("--model-constraint", type=json.loads, help="Route constraints as a JSON object")
    run_parser = subcommands.add_parser("run", help="Run one executor on a brief in a chosen worktree")
    run_parser.add_argument("--tool", required=True, choices=("codex", "claude-code", "cursor"))
    for name in ("model", "effort", "brief", "workdir"):
        run_parser.add_argument("--" + name, required=True)
    run_parser.add_argument("--profile", choices=("review", "write"), default="review")
    run_parser.add_argument("--run-id")
    run_parser.add_argument("--timeout-min", type=_timeout, default=30)
    run_parser.add_argument("--stall-min", type=_timeout, default=5)
    run_parser.add_argument("--json", action="store_true")
    trust_parser = subcommands.add_parser("trust", help="List or clean recorded Codex/Cursor trust entries")
    trust_commands = trust_parser.add_subparsers(dest="trust_command", required=True)
    for name in ("list", "clean"):
        command = trust_commands.add_parser(name)
        command.add_argument("--repo", default=None, help="Repository (default: current directory)")
        command.add_argument("--codex-home", default=None, help="Codex home (default: CODEX_HOME or ~/.codex)")
        command.add_argument("--json", action="store_true")
        if name == "clean":
            mode = command.add_mutually_exclusive_group()
            mode.add_argument("--dry-run", action="store_true", help="Preview only (default)")
            mode.add_argument("--apply", action="store_true")
            command.add_argument("--yes", action="store_true", help="Confirm without a TTY")
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return exitcodes.USAGE

    if args.command == "do":
        from orcexe.do import run_do
        summary = run_do(args.task, args.repo, dry_run=args.dry_run, timeout=args.timeout,
                         model_constraint=args.model_constraint)
        print(json.dumps(summary, ensure_ascii=False) if args.json else summary["message"],
              end="\n" if args.json else "")
        return summary["exit_code"]

    if args.command == "trust":
        return _trust_command(args)

    if args.command == "intake":
        from orcexe.intake import intake_command
        return intake_command(args)

    if args.command == "map":
        from orcexe.mapping import map_command
        return map_command(args)

    if args.command == "status":
        from orcexe.planrun import status_command
        return status_command(args)

    if args.command == "plan":
        if args.resume and args.run is None:
            parser.error("--resume requires --run")
        if args.run is not None:
            from orcexe.planrun import run_command
            return run_command(args)
        from orcexe.planning import plan_command
        return plan_command(args)

    if args.command == "task":
        from orcexe.task import task_command
        return task_command(args)

    if args.command in ("questions", "answer"):
        from orcexe.questions import questions_command, answer_command
        return questions_command(args) if args.command == "questions" else answer_command(args)

    if args.command == "history":
        from orcexe.history import history_command
        return history_command(args)

    if args.command == "slop":
        from orcexe.slop import slop_command
        return slop_command(args)

    if args.command == "run":
        from orcexe.run import run_command
        return run_command(args)

    if args.command == "doctor":
        from orcexe.doctor import doctor_command
        return doctor_command(args)

    if args.command == "probe":
        from orcexe.doctor import probe_command
        return probe_command(args)

    manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
    with manifest.open(encoding="utf-8") as handle:
        version = json.load(handle)["version"]
    if args.json:
        print(json.dumps({"name": "orcexe", "version": version}))
    else:
        print("orcexe {}".format(version))
    return exitcodes.OK
