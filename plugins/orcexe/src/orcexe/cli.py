"""Command-line entry point for orcexe."""

import argparse
import json
import math
from pathlib import Path

from orcexe import exitcodes


def _timeout(value):
    try:
        result = float(value)
        if math.isfinite(result) and result > 0:
            return result
    except ValueError:
        pass
    raise argparse.ArgumentTypeError("timeout must be finite and positive")


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

    manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
    with manifest.open(encoding="utf-8") as handle:
        version = json.load(handle)["version"]
    if args.json:
        print(json.dumps({"name": "orcexe", "version": version}))
    else:
        print("orcexe {}".format(version))
    return exitcodes.OK
