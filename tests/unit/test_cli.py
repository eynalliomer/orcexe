"""Exercise CLI behavior through its public entry points."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "orcexe"
LAUNCHER = PLUGIN / "bin" / "orcexe"


class CliTests(unittest.TestCase):
    def setUp(self):
        with (PLUGIN / ".claude-plugin" / "plugin.json").open(encoding="utf-8") as handle:
            self.version = json.load(handle)["version"]

    def run_module(self, *args):
        return subprocess.run(
            [sys.executable, "-B", "-m", "orcexe"] + list(args),
            cwd=str(PLUGIN / "src"), capture_output=True, text=True,
        )

    def assert_version(self, result):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "orcexe {}\n".format(self.version))
        self.assertEqual(result.stderr, "")

    def test_version(self):
        self.assert_version(self.run_module("version"))

    def test_version_json(self):
        result = self.run_module("version", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {"name": "orcexe", "version": self.version})
        self.assertTrue(result.stdout.endswith("\n"))
        self.assertEqual(result.stderr, "")

    def test_no_subcommand(self):
        result = self.run_module()
        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: orcexe", result.stdout)

    def test_unknown_subcommand(self):
        result = self.run_module("unknown")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice", result.stderr)

    def test_help(self):
        result = self.run_module("--help")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: orcexe", result.stdout)
        self.assertIn("version", result.stdout)

    def test_launcher_from_other_cwd(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
            command = [str(LAUNCHER), "version"]
            if sys.platform == "win32":
                command.insert(0, sys.executable)
            result = subprocess.run(command, cwd=directory, capture_output=True, text=True)
        self.assert_version(result)

    @unittest.skipIf(sys.platform == "win32", "Symlink launcher test requires POSIX")
    def test_launcher_via_symlink(self):
        with tempfile.TemporaryDirectory(dir=str(ROOT)) as directory:
            link = Path(directory) / "orcexe"
            link.symlink_to(LAUNCHER)
            result = subprocess.run(
                [str(link), "version"], cwd=directory, capture_output=True, text=True,
            )
        self.assert_version(result)
