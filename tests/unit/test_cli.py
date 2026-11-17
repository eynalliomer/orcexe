"""Exercise CLI behavior through its public entry points."""

import json
import io
import os
from contextlib import redirect_stdout
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins" / "orcexe"
LAUNCHER = PLUGIN / "bin" / "orcexe"


class CliTests(unittest.TestCase):
    def test_doctor_arguments(self):
        from orcexe import cli
        with mock.patch("orcexe.doctor.doctor_command", return_value=1) as command:
            self.assertEqual(cli.main(["doctor", "--json", "--quick", "--tool", "cursor", "--no-cache"]), 1)
        args = command.call_args.args[0]
        self.assertTrue(args.json and args.quick and args.no_cache)
        self.assertEqual(args.tool, "cursor")
        with mock.patch("orcexe.doctor.doctor_command", return_value=0) as command:
            self.assertEqual(cli.main(["doctor"]), 0)
        args = command.call_args.args[0]
        self.assertFalse(args.json or args.quick or args.no_cache)
        self.assertIsNone(args.tool)

    def test_probe_arguments_and_invalid_commands(self):
        from orcexe import cli
        with mock.patch("orcexe.doctor.probe_command", return_value=2) as command:
            self.assertEqual(cli.main(["probe", "codex", "auth", "--json"]), 2)
        args = command.call_args.args[0]
        self.assertEqual((args.tool, args.operation, args.json), ("codex", "auth", True))
        for args in (["probe"], ["probe", "codex", "run"], ["probe", "other", "detect"],
                     ["doctor", "--tool", "other"], ["doctor", "--fix"]):
            with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                cli.main(args)
            self.assertEqual(error.exception.code, 2)

    def test_do_argument_parsing(self):
        from orcexe import cli
        summary = {"exit_code": 5, "message": "quota\n"}
        with mock.patch("orcexe.do.run_do", return_value=summary) as run, \
                mock.patch("builtins.print") as output:
            self.assertEqual(cli.main(["do", "a task", "--repo", "/example", "--dry-run",
                "--json", "--timeout", "12.5", "--model-constraint", '{"deny_models":["x"]}']), 5)
        run.assert_called_once_with("a task", "/example", dry_run=True, timeout=12.5,
                                    model_constraint={"deny_models": ["x"]})
        self.assertEqual(json.loads(output.call_args.args[0]), summary)

    def test_do_argument_defaults(self):
        from orcexe import cli
        with mock.patch("orcexe.do.run_do", return_value={"exit_code": 0, "message": "plan\n"}) as run, \
                mock.patch("builtins.print"):
            self.assertEqual(cli.main(["do", "task"]), 0)
        run.assert_called_once_with("task", None, dry_run=False, timeout=1800, model_constraint=None)

    def test_do_rejects_missing_task_and_bad_timeout(self):
        for arguments in (["do"], ["do", "task", "--timeout", "0"],
                          ["do", "task", "--timeout", "nan"], ["do", "task", "--timeout", "-1"]):
            with self.subTest(arguments=arguments):
                result = self.run_module(*arguments)
                self.assertEqual(result.returncode, 2)

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


class TrustCliTests(unittest.TestCase):
    def setUp(self):
        from orcexe import trust
        self.tmp = tempfile.TemporaryDirectory(dir=str(ROOT), prefix="orcexe-trust-cli-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.codex = self.home / "codex"
        self.codex.mkdir(parents=True)
        self.config = self.codex / "config.toml"
        self.ledger = self.root / ".orcexe/trust-ledger.json"
        self.key = str(self.root / "gone")
        self.config.write_text('[projects.' + json.dumps(self.key) + ']\ntrust_level = "trusted"\n')
        patch = mock.patch.dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.codex))
        patch.start()
        self.addCleanup(patch.stop)
        trust.record(self.ledger, frozenset(), frozenset({self.key}), "run", self.config)
        patch = mock.patch("orcexe.gitutil.main_worktree", return_value=str(self.root))
        self.main_worktree = patch.start()
        self.addCleanup(patch.stop)

    def invoke(self, *arguments):
        from orcexe import cli
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["trust"] + list(arguments))
        return code, output.getvalue()

    def test_list_and_repo_codex_home_options(self):
        code, output = self.invoke("list", "--repo", str(self.root), "--codex-home", str(self.codex), "--json")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["entries"][0]["key"], self.key)
        self.main_worktree.assert_called_with(str(self.root))
        self.assertIn("present " + self.key, self.invoke("list")[1])

    def test_dry_run_refusal_apply_and_noop_exit_codes(self):
        for arguments in (("clean",), ("clean", "--dry-run", "--yes")):
            code, output = self.invoke(*arguments, "--json")
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output)["candidates"], [self.key])
            self.assertTrue(self.config.read_bytes())
        with mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("builtins.input", side_effect=AssertionError("no TTY")):
            code, output = self.invoke("clean", "--apply", "--json")
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output)["confirmed"])
        code, output = self.invoke("clean", "--apply", "--yes")
        self.assertEqual(code, 0)
        self.assertIn(self.key, output)
        self.assertIn("Backup: ~/codex/config.toml.orcexe.bak", output)
        self.assertEqual(self.config.read_bytes(), b'')
        self.assertEqual(self.invoke("clean", "--apply")[0], 0)

    def test_interactive_confirmation_once_and_json_stdout(self):
        for answer, expected in (("no", 1), ("y", 0)):
            with mock.patch("sys.stdin.isatty", return_value=True), \
                    mock.patch("builtins.input", return_value=answer) as prompt, \
                    mock.patch("sys.stderr", new_callable=io.StringIO):
                code, output = self.invoke("clean", "--apply", "--json")
            self.assertEqual(code, expected)
            self.assertEqual(json.loads(output)["confirmed"], expected == 0)
            prompt.assert_called_once_with()

    def test_corrupt_ledger_unreadable_config_and_changed_exit_two(self):
        original = self.config.read_bytes()
        def concurrent_change():
            self.config.write_bytes(original + b'# concurrent change\n')
            return "y"
        with mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("sys.stderr", new_callable=io.StringIO), \
                mock.patch("builtins.input", side_effect=concurrent_change):
            code, output = self.invoke("clean", "--apply", "--json")
        self.assertEqual(code, 2)
        self.assertIn("changed during clean", json.loads(output)["error"])
        self.assertEqual(self.config.read_bytes(), original + b'# concurrent change\n')
        self.assertFalse(list(self.codex.glob('*.bak*')))
        self.config.unlink()
        self.config.mkdir()
        self.assertEqual(self.invoke("clean")[0], 2)
        self.assertEqual(self.invoke("list")[0], 2)
        self.ledger.write_text('{"malformed":true}')
        for command in ("list", "clean"):
            code, output = self.invoke(command, "--json")
            self.assertEqual(code, 2)
            self.assertIn("error", json.loads(output))

    def test_config_and_ledger_write_failure_exit_two(self):
        for target in ("_replace", "fsio.atomic_write_json"):
            with self.subTest(target=target), mock.patch("orcexe.trust." + target, side_effect=OSError("unwritable")):
                self.assertEqual(self.invoke("clean", "--apply", "--yes")[0], 2)

    def test_mutually_exclusive_flags_and_missing_subcommand(self):
        from orcexe import cli
        for arguments in (["trust"], ["trust", "clean", "--dry-run", "--apply"]):
            with mock.patch("sys.stderr", new_callable=io.StringIO), self.assertRaises(SystemExit) as error:
                cli.main(arguments)
            self.assertEqual(error.exception.code, 2)
