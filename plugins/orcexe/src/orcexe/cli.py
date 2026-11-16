"""Command-line entry point for orcexe."""

import argparse
import json
from pathlib import Path

from orcexe import exitcodes


def main(argv=None):
    """Run the CLI and return its exit status."""
    parser = argparse.ArgumentParser(prog="orcexe", description="Orchestrate AI coding tools.")
    subcommands = parser.add_subparsers(dest="command")
    version_parser = subcommands.add_parser("version", help="Show the orcexe version")
    version_parser.add_argument("--json", action="store_true", help="Print version as JSON")
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return exitcodes.USAGE

    manifest = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
    with manifest.open(encoding="utf-8") as handle:
        version = json.load(handle)["version"]
    if args.json:
        print(json.dumps({"name": "orcexe", "version": version}))
    else:
        print("orcexe {}".format(version))
    return exitcodes.OK
