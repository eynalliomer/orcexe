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
        print("Remove Codex trust entries:\n" + "\n".join(keys), file=sys.stderr)
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
            for label in ("candidates", "kept_live", "skipped_edited", "removed"):
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
    do_parser = subcommands.add_parser("do", help="Run one fast-lane task in an isolated worktree")
    do_parser.add_argument("task")
    do_parser.add_argument("--repo", default=None, help="Repository (default: current directory)")
    do_parser.add_argument("--dry-run", action="store_true")
    do_parser.add_argument("--json", action="store_true")
    do_parser.add_argument("--timeout", type=_timeout, default=1800)
    do_parser.add_argument("--model-constraint", type=json.loads, help="Route constraints as a JSON object")
    trust_parser = subcommands.add_parser("trust", help="List or clean recorded Codex trust entries")
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

    manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
    with manifest.open(encoding="utf-8") as handle:
        version = json.load(handle)["version"]
    if args.json:
        print(json.dumps({"name": "orcexe", "version": version}))
    else:
        print("orcexe {}".format(version))
    return exitcodes.OK
