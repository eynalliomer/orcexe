"""Real, hermetic fast-lane observations; no AI executables or network."""

from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import shutil
import subprocess
import py_compile
import importlib.util
import hashlib
import sys
import time
import unittest
from unittest import mock

from tests.unit._timing import scaled
from tests.unit._fixtures import git_environment
from tests.unit.test_gitutil import GitFixture
from tests.unit.test_run_result import valid_result
from orcexe import gate_lite as gl, worktree
from orcexe.classify import Classification


def _native_git(fixture):
    fixture.git = git_environment(fixture.env, fixture.directory)


class GateTests(GitFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Build once; each scenario receives an independent copy of both Git trees.
        fixture = cls.shared = GitFixture()
        fixture.templates = cls.templates
        fixture.setUp()
        _native_git(fixture)
        cls.addClassCleanup(fixture.doCleanups)
        cls.write(fixture.main, "value.py", "VALUE = 1\n")
        cls.write(fixture.main, "other.py", "OTHER = 1\n")
        cls.write(fixture.main, "tests/__init__.py", "")
        cls.write(fixture.main, "tests/test_value.py", "import unittest\nfrom value import VALUE\n"
                   "class Tests(unittest.TestCase):\n    def test_value(self):\n"
                   "        self.assertGreater(VALUE, 0)\n")
        cls.write(fixture.main, ".gitignore", "ignored*\n__pycache__/\n")
        fixture.git_run(fixture.main, "add", ".")
        fixture.git_run(fixture.main, "commit", "--quiet", "-m", "seed")
        with mock.patch.dict(os.environ, fixture.env, clear=True):
            cls.shared_wt = worktree.create(fixture.main, worktree.Spec("plan", "task", "fake"))

    def setUp(self):
        self.copy_fixture(self.shared)
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.shared_wt = replace(self.shared_wt, path=str(
            self.directory / Path(self.shared_wt.path).relative_to(self.shared.directory)))
        self.wt = self.shared_wt
        self.path = Path(self.wt.path)
        override = self.main / ".orcexe/commands.yaml"
        if override.exists():
            override.unlink()
        self.run_dir = self.main / ".orcexe" / "runs" / "T1-r1"
        self.classification = Classification("write", "fast", ("value.py",), ())
        self.run_result = valid_result(self.run_dir)
        self.marker = self.directory / "command-ran"
        if self.marker.exists():
            self.marker.unlink()
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        self.marker_command = [gl.Command("marker", [sys.executable, "-c",
                               "print('command-ran')"])]

    @staticmethod
    def write(root, name, text):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def edit(self, text="VALUE = 2\n", name="value.py"):
        self.write(self.path, name, text)

    def call(self, **kwargs):
        return gl.gate(repo_path=self.main, worktree=self.wt,
                       controls_before=kwargs.pop("controls_before", self.controls_before),
                       classification=self.classification, run_result=self.run_result,
                       run_dir=self.run_dir, command_timeout_s=kwargs.pop("command_timeout_s", scaled(5)),
                       total_timeout_s=kwargs.pop("total_timeout_s", scaled(15)), **kwargs)

    def outcome(self, result, verdict, failed=None, status="fail"):
        self.assertEqual(result["verdict"], verdict, result)
        gl.validate_gate_result(result)
        self.assertEqual(json.loads((self.run_dir / "gate.json").read_text()), result)
        checks = result["checks"]
        if failed:
            index = next(i for i, check in enumerate(checks) if check["id"] == failed)
            self.assertEqual(checks[index]["status"], status, result)
            self.assertTrue(all(c["status"] == "skipped" for c in checks[index + 1:]))
        else:
            self.assertTrue(all(c["status"] == "pass" for c in checks))

    def test_happy_path_detects_and_runs_real_unittest(self):
        self.edit("VALUE = 1 # edit\n")
        result = self.call()
        self.outcome(result, "accepted")
        self.assertEqual((result["diff"]["added"], result["diff"]["deleted"]), (1, 1))
        self.assertIn("Ran 1 test", (self.run_dir / "gate" / "unittest.log").read_text())

    def test_run_status_stops_every_command(self):
        self.edit("VALUE = 1 # edit\n")
        for status in ("quota", "auth", "timeout", "stalled", "cancelled", "blocked", "failed", "completed", "invalid"):
            with self.subTest(status=status):
                self.run_result.update(status=status, warnings=["empty_diff"] if status == "completed" else [])
                self.outcome(self.call(commands=self.marker_command), "failed", "run_status")
                self.assertFalse(self.marker.exists())
        self.run_result = {"run_id": "invalid"}
        self.outcome(self.call(commands=self.marker_command), "failed", "run_status")

    def test_empty_diff(self):
        result = self.call(commands=self.marker_command)
        self.outcome(result, "failed", "diff_collect")
        self.assertEqual(result["checks"][2]["detail"], "empty_diff")
        self.assertFalse(self.marker.exists())

    def test_retry_gates_empty_diff_warning_on_cumulative_tree(self):
        # The executor found the earlier attempt's edit and changed nothing more.
        self.edit("VALUE = 1 # edit\n")
        self.run_result.update(warnings=["empty_diff"])
        self.outcome(self.call(commands=self.marker_command), "failed", "run_status")
        self.assertFalse(self.marker.exists())
        result = self.call(commands=self.marker_command, retry=True)
        self.outcome(result, "accepted")
        self.assertEqual(result["diff"]["target_path"], "value.py")
        self.assertIn("command-ran", (self.run_dir / "gate/marker.log").read_text())

    def test_retry_on_never_changed_tree_is_not_accepted(self):
        self.run_result.update(warnings=["empty_diff"])
        result = self.call(commands=self.marker_command, retry=True)
        self.outcome(result, "failed", "diff_collect")
        self.assertEqual(result["checks"][2]["detail"], "empty_diff")
        self.assertFalse(self.marker.exists())

    def test_retry_still_runs_later_checks_on_cumulative_tree(self):
        self.run_result.update(warnings=["empty_diff"])
        self.edit("VALUE = 1 # edit \n")
        self.outcome(self.call(commands=self.marker_command, retry=True), "needs_fix", "diff_check")
        self.edit("VALUE = 2\n")
        self.outcome(self.call(commands=self.marker_command, retry=True), "escalated", "code_unchanged")
        self.assertFalse(self.marker.exists())
        for status in ("failed", "timeout"):
            with self.subTest(status=status):
                self.run_result.update(status=status, warnings=[])
                self.outcome(self.call(commands=self.marker_command, retry=True), "failed", "run_status")

    def test_whitespace_and_conflict_markers(self):
        self.git_run(self.path, "config", "core.whitespace", "-blank-at-eol")
        self.addCleanup(self.git_run, self.path, "config", "--unset", "core.whitespace")
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        for content in ("VALUE = 1 # edit \n", "<<<<<<< ours\nVALUE = 1 # edit\n=======\nVALUE = 1 # three\n>>>>>>> theirs\n"):
            with self.subTest(content=content):
                self.edit(content)
                self.outcome(self.call(commands=self.marker_command), "needs_fix", "diff_check")
                self.assertFalse(self.marker.exists())

    def test_scope_second_new_deleted_rename_mode_symlink_binary(self):
        scenarios = ("second", "new", "deleted", "rename", "chmod", "symlink", "binary")
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.git_run(self.path, "reset", "--hard", self.wt.base_commit)
                self.git_run(self.path, "clean", "-fdx")
                self.edit("VALUE = 1 # edit\n")
                if scenario == "second":
                    self.edit("OTHER = 2\n", "other.py")
                elif scenario == "new":
                    self.edit("new\n", "new.txt")
                elif scenario == "deleted":
                    (self.path / "value.py").unlink()
                elif scenario == "rename":
                    # Exact rename, so both sides must be collected explicitly.
                    self.edit("VALUE = 1\n")
                    (self.path / "value.py").rename(self.path / "renamed.py")
                elif scenario == "chmod":
                    (self.path / "value.py").chmod(0o755)
                elif scenario == "symlink":
                    (self.path / "value.py").unlink()
                    (self.path / "value.py").symlink_to("other.py")
                else:
                    (self.path / "value.py").write_bytes(b"\0binary\x80\n")
                result = self.call(commands=self.marker_command)
                self.outcome(result, "escalated", "scope")
                self.assertEqual(result["escalation"], "lane")
                self.assertFalse(self.marker.exists())
                files = result["diff"]["files"]
                if scenario == "rename":
                    self.assertEqual((files[0]["old_path"], files[0]["path"]), ("value.py", "renamed.py"))
                elif scenario in ("symlink", "binary"):
                    self.assertTrue(files[0][scenario])

    def test_51_lines_exceed_budget_and_50_is_allowed(self):
        self.edit("VALUE = 1 # edit\n" + "# comment\n" * 49)  # 50 added, one deleted.
        self.outcome(self.call(commands=self.marker_command), "escalated", "size")
        self.assertFalse(self.marker.exists())
        self.edit("VALUE = 1 # edit\n" + "# comment\n" * 48)
        self.outcome(self.call(commands=self.marker_command), "accepted")

    def test_secret_added_line_redacted_even_with_leading_pluses(self):
        self.git_run(self.path, "config", "diff.outputIndicatorNew", ">")
        self.addCleanup(self.git_run, self.path, "config", "--unset", "diff.outputIndicatorNew")
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        key = "sk" + "-" + "A1b2C3d4" * 6
        for prefix in ("# ", "# ++"):
            with self.subTest(prefix=prefix):
                self.edit("VALUE = 1 # edit\n" + prefix + key + "\n")
                result = self.call(commands=self.marker_command)
                self.outcome(result, "needs_fix", "secrets")
                self.assertIn("secret_suspected", result["warnings"])
                self.assertNotIn(key, (self.run_dir / "gate.json").read_text())
                self.assertEqual(result["checks"][7]["detail"], "value.py:2")
                self.assertFalse(self.marker.exists())

    def test_removed_tests_and_added_skip(self):
        self.classification = Classification("write", "fast", ("tests/test_value.py",), ())
        original = (self.path / "tests/test_value.py").read_text()
        for content in (original.replace("def test_value", "def helper"),
                        original.replace("    def test_value", "    @unittest.skip('reason')\n    def test_value")):
            with self.subTest(content=content):
                self.edit(content, "tests/test_value.py")
                self.outcome(self.call(commands=self.marker_command), "escalated", "code_unchanged")
                self.assertFalse(self.marker.exists())

    def test_project_failure(self):
        self.edit("VALUE = 1 # edit\n")
        self.outcome(self.call(commands=[gl.Command("unittest", [sys.executable, "-c", "import sys; print('FAILED'); sys.exit(1)"])]), "needs_fix", "project_checks")
        self.assertIn("FAILED", (self.run_dir / "gate/unittest.log").read_text())

    def test_project_timeout_and_total_budget(self):
        self.edit("VALUE = 1 # edit\n")
        spin = gl.Command("spin", [sys.executable, "-c", "while True: pass"])
        start = time.monotonic()
        result = self.call(commands=[spin], command_timeout_s=scaled(.12))
        self.outcome(result, "needs_fix", "project_checks")
        self.assertLess(time.monotonic() - start, scaled(5))
        # The detail names the per-command limit that was hit.
        self.assertEqual(result["checks"][-1]["detail"],
                         "spin: timeout ({:g}s)".format(round(scaled(.12), 3)))
        # Exercise the project-stage total budget independently of Git startup.
        status, detail = gl._project([spin], self.path, self.run_dir / "gate",
                                     scaled(5), time.monotonic() + scaled(.12))
        self.assertEqual(status, "fail")
        self.assertIn("timeout", detail)
        self.assertEqual(detail, "spin: total_timeout")
        self.outcome(self.call(commands=self.marker_command, total_timeout_s=scaled(.000001)),
                     "unverified", "diff_collect", "not_seen")
        self.assertFalse(self.marker.exists())

    def test_no_commands_and_missing_binary(self):
        self.edit("VALUE = 1 # edit\n")
        self.outcome(self.call(commands=[]), "unverified", "project_checks", "not_seen")
        self.outcome(self.call(commands=[gl.Command("absent", ["orcexe-binary-that-does-not-exist"])]),
                     "unverified", "project_checks", "not_seen")
        self.assertIn("absent: not_seen", (self.run_dir / "gate.json").read_text())

    def test_command_sources_are_protected_with_handbuilt_classification(self):
        # Add base definitions to a separate base, then create a fresh managed worktree.
        self.write(self.main, "package.json", '{"scripts":{"test":"echo test"}}\n')
        self.write(self.main, "Makefile", "test:\n\t@echo test\n")
        self.git_run(self.main, "add", "package.json", "Makefile")
        self.git_run(self.main, "commit", "--quiet", "-m", "definitions")
        self.wt = worktree.create(self.main, worktree.Spec("plan", "sources", "fake"))
        self.path = Path(self.wt.path)
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        for name in ("package.json", "Makefile"):
            with self.subTest(name=name):
                self.git_run(self.path, "reset", "--hard", self.wt.base_commit)
                self.classification = Classification("write", "fast", (name,), ())
                content = (self.path / name).read_text().replace("test", "lint")
                self.edit(content, name)
                self.outcome(self.call(commands=self.marker_command), "escalated", "scope")
                self.assertFalse(self.marker.exists())

    def test_invalid_yaml_has_only_key_names(self):
        self.edit("VALUE = 1 # edit\n")
        invalid = (("version: 2\ncommands: []\n", "version"),
                   ("version: 1\ncommands: [{id: test, argv: nope}]\n", "argv"),
                   ("version: 1\n", "commands"),
                   ("version: 1\ncommands: [{argv: [python3]}]\n", "id"),
                   ("version: 1\ncommands: [{id: x, argv: [python3], extra: 1}]\n", "extra"),
                   ("version: 1\ncommands: []\nextra: 1\n", "extra"),
                   ("version: 1\ncommands: [\n", "commands.yaml"))
        for text, key in invalid:
            with self.subTest(key=key):
                self.write(self.main, ".orcexe/commands.yaml", text)
                with self.assertRaisesRegex(ValueError, key):
                    gl.detect_commands(self.main, self.wt.base_commit)
        self.write(self.main, ".orcexe/commands.yaml", invalid[1][0])
        result = self.call()
        self.outcome(result, "needs_fix", "project_checks")
        self.assertIn("argv", result["checks"][-1]["detail"])

    def test_index_unchanged_committed_staged_and_unstaged_are_combined(self):
        self.edit("VALUE = 1 # three\n")
        self.git_run(self.path, "add", "value.py")
        self.git_run(self.path, "commit", "--quiet", "-m", "executor")
        self.edit("VALUE = 1 # four\n")
        self.git_run(self.path, "add", "value.py")
        self.edit("VALUE = 1 # edit\n")
        index = Path(self.git_run(self.path, "rev-parse", "--git-path", "index").strip())
        before_bytes = index.read_bytes()
        before_names = self.git_run(self.path, "diff", "--cached", "--name-only")
        result = self.call()
        self.outcome(result, "accepted")
        self.assertNotEqual(result["diff"]["head"], self.wt.base_commit)
        self.assertEqual(index.read_bytes(), before_bytes)
        self.assertEqual(self.git_run(self.path, "diff", "--cached", "--name-only"), before_names)
        self.assertEqual([f["path"] for f in result["diff"]["files"]], ["value.py"])

    def test_p1_committed_out_of_scope_restored_on_disk(self):
        self.edit("OTHER = 666\n", "other.py")
        self.git_run(self.path, "commit", "-qam", "executor")
        self.edit("OTHER = 1\n", "other.py")
        self.edit("VALUE = 1 # edit\n")
        result = self.call(commands=self.marker_command)
        self.outcome(result, "escalated", "scope")
        self.assertIn("other.py", [f["path"] for f in result["diff"]["files"]])
        self.assertFalse(self.marker.exists())

    def test_committed_out_of_scope_restored_in_index_and_worktree(self):
        self.edit("OTHER = 666\n", "other.py")
        self.git_run(self.path, "commit", "-qam", "executor")
        self.git_run(self.path, "reset", self.wt.base_commit, "--", "other.py")
        self.edit("OTHER = 1\n", "other.py")
        self.edit("VALUE = 1 # edit\n")
        self.outcome(self.call(commands=self.marker_command), "escalated", "scope")
        self.assertFalse(self.marker.exists())

    def test_p2_self_ignoring_test_never_executes(self):
        self.edit("VALUE = 1 # edit\n")
        self.edit("*\n", "tests/.gitignore")
        self.edit("from pathlib import Path\nPath(" + repr(str(self.marker)) + ").touch()\n",
                  "tests/test_zz_evil.py")
        result = self.call()  # Real base detection selects unittest discover.
        self.outcome(result, "escalated", "scope")
        self.assertEqual(result["checks"][4]["detail"], "untracked_ignored:2")
        self.assertFalse(self.marker.exists())

    def test_p3_ignored_configuration_never_executes_commands(self):
        self.edit("VALUE = 1 # edit\n")
        self.edit("X = 1\n", "ignored_conf.py")
        result = self.call(commands=self.marker_command)
        self.outcome(result, "escalated", "scope")
        self.assertEqual(result["checks"][4]["detail"], "untracked_ignored:1")
        self.assertFalse(self.marker.exists())

    def test_p4_staged_out_of_scope_restored_on_disk(self):
        self.edit("VALUE = 1 # edit\n")
        self.edit("OTHER = 7\n", "other.py")
        self.git_run(self.path, "add", "other.py")
        self.edit("OTHER = 1\n", "other.py")
        index = Path(self.git_run(self.path, "rev-parse", "--git-path", "index").strip())
        before = index.read_bytes()
        self.outcome(self.call(commands=self.marker_command), "escalated", "scope")
        self.assertEqual(index.read_bytes(), before)
        self.assertFalse(self.marker.exists())

    def test_p5_no_final_newline(self):
        self.edit("VALUE = 1 # edit")
        self.outcome(self.call(commands=self.marker_command), "accepted")
        self.assertIn('command-ran', (self.run_dir / 'gate/marker.log').read_text())

    def test_committed_target_reverted_and_reapplied_identically(self):
        self.edit("VALUE = 1 # edit\n")
        self.git_run(self.path, "commit", "-qam", "executor")
        self.git_run(self.path, "revert", "--no-edit", "HEAD")
        self.edit("VALUE = 1 # edit\n")
        self.git_run(self.path, "commit", "-qam", "reapply")
        result = self.call(commands=self.marker_command)
        self.outcome(result, "accepted")
        self.assertEqual((result["diff"]["added"], result["diff"]["deleted"]), (2, 2))
        self.assertIn('command-ran', (self.run_dir / 'gate/marker.log').read_text())

    def test_tool_cache_allow_list(self):
        self.edit("VALUE = 1 # edit\n")
        for name in ("__pycache__/x.cpython-39.pyc", "nested/__pycache__/x.pyc",
                     ".pytest_cache/v/cache/nodeids", ".mypy_cache/3.9/x.json",
                     ".ruff_cache/0.1/data"):
            self.edit("cache\n", name)
        self.outcome(self.call(commands=self.marker_command), "accepted")
        self.assertIn('command-ran', (self.run_dir / 'gate/marker.log').read_text())

    def test_cache_exemptions_are_closed_and_do_not_follow_symlinks(self):
        self.edit("VALUE = 1 # edit\n")
        for name in ("__pycache__/config.py", "__pycache__/x.pyc/config.py", "x.pyc",
                     ".pytest_cache/.gitignore", ".mypy_cache/.gitattributes",
                     "node_modules/config.js", ".env", "build/config.py", "dist/config.py",
                     "x.egg-info/config.py", ".pytest_cache-link"):
            with self.subTest(name=name):
                self.git_run(self.path, "clean", "-fdx")
                self.edit("unsafe\n", name)
                self.outcome(self.call(commands=self.marker_command), "escalated", "scope")
                self.assertFalse(self.marker.exists())
        self.git_run(self.path, "clean", "-fdx")
        (self.path / ".pytest_cache").symlink_to(self.home, target_is_directory=True)
        self.outcome(self.call(commands=self.marker_command), "escalated", "scope")
        self.assertFalse(self.marker.exists())

    def test_head_and_index_target_variants_are_checked(self):
        original_test = (self.path / "tests/test_value.py").read_text()
        key = "sk" + "-" + "A1b2C3d4" * 6
        scenarios = (("VALUE = 1 # three \n", "value.py", "needs_fix", "diff_check"),
                     ("VALUE = 1 # three\n# " + key + "\n", "value.py", "needs_fix", "secrets"),
                     (original_test.replace("def test_value", "def helper"),
                      "tests/test_value.py", "escalated", "code_unchanged"),
                     ("VALUE = 1 # three\n" + "# long\n" * 49, "value.py", "escalated", "size"))
        for state in ("head", "index"):
            for content, name, verdict, check in scenarios:
                with self.subTest(state=state, check=check):
                    self.git_run(self.path, "reset", "--hard", self.wt.base_commit)
                    self.classification = Classification("write", "fast", (name,), ())
                    self.edit(content, name)
                    self.git_run(self.path, "add", name)
                    if state == "head":
                        self.git_run(self.path, "commit", "--quiet", "-m", "executor")
                        self.git_run(self.path, "reset", self.wt.base_commit, "--", name)
                    self.edit(original_test + "# safe\n" if name.startswith("tests/") else "VALUE = 1 # edit\n", name)
                    self.outcome(self.call(commands=self.marker_command), verdict, check)
                    self.assertFalse(self.marker.exists())

    def test_distinct_target_versions_share_line_budget(self):
        self.edit("VALUE = 1 # three\n" + "# head\n" * 24)
        self.git_run(self.path, "commit", "-qam", "executor")
        self.edit("VALUE = 1 # four\n" + "# index\n" * 24)
        self.git_run(self.path, "add", "value.py")
        self.edit("VALUE = 1 # edit\n")
        result = self.call(commands=self.marker_command)
        self.outcome(result, "escalated", "size")
        self.assertEqual(result["diff"]["added"] + result["diff"]["deleted"], 54)
        self.assertFalse(self.marker.exists())

    def test_replaced_root_git_metadata_is_never_followed(self):
        self.edit("VALUE = 1 # edit\n")
        gitfile = self.path / ".git"
        original = gitfile.read_bytes()
        for replacement in ("directory", "different_gitfile", "symlink"):
            with self.subTest(replacement=replacement):
                gitfile.unlink()
                try:
                    if replacement == "directory":
                        gitfile.mkdir()
                        (gitfile / "config").write_text("invalid git config\n")
                    elif replacement == "different_gitfile":
                        gitfile.write_text("gitdir: " + str(self.main / ".git") + "\n")
                    else:
                        target = self.directory / "replacement-gitfile"
                        target.write_bytes(original)
                        gitfile.symlink_to(target)
                    with mock.patch.object(gl, "_git", wraps=gl._git) as observed:
                        result = self.call(commands=self.marker_command)
                    self.outcome(result, "escalated", "controls")
                    self.assertFalse(observed.called)
                    self.assertTrue(all(Path(c.args[0]) != self.path for c in observed.call_args_list))
                    self.assertFalse(self.marker.exists())
                finally:
                    if gitfile.is_dir() and not gitfile.is_symlink():
                        shutil.rmtree(gitfile)
                    else:
                        gitfile.unlink()
                    gitfile.write_bytes(original)

    def test_nested_git_metadata_including_ignored_empty_directory(self):
        self.edit("VALUE = 1 # edit\n")
        for name in ("nested/.git", "ignored-repo/.git", ".pytest_cache/nested/.git"):
            with self.subTest(name=name):
                self.git_run(self.path, "clean", "-ffdx")
                nested = self.path / name
                nested.mkdir(parents=True)
                with mock.patch.object(gl, "_git", wraps=gl._git) as observed:
                    result = self.call(commands=self.marker_command)
                self.outcome(result, "escalated", "scope")
                self.assertEqual(result["checks"][4]["detail"], "nested_git_metadata")
                self.assertTrue(all(Path(c.args[0]) in (self.main, self.path)
                                    for c in observed.call_args_list))
                self.assertFalse(self.marker.exists())
                self.assertFalse(any("--others" in c.args for c in observed.call_args_list))

    def test_git_controls_anywhere_cannot_be_allowed_targets(self):
        for name in (".gitignore", "nested/.gitignore", "nested/.gitattributes"):
            with self.subTest(name=name):
                self.git_run(self.path, "reset", "--hard", self.wt.base_commit)
                self.git_run(self.path, "clean", "-ffdx")
                self.edit("# trusted\n", name)
                self.git_run(self.path, "add", name)
                self.git_run(self.path, "commit", "--quiet", "-m", "trusted control")
                base = self.git_run(self.path, "rev-parse", "HEAD").strip()
                self.wt = replace(self.shared_wt, base_commit=base)
                self.classification = Classification("write", "fast", (name,), ())
                self.edit("# changed\n", name)
                self.outcome(self.call(commands=self.marker_command), "escalated", "scope")
                self.assertFalse(self.marker.exists())

    def test_logs_redacted_bounded_and_child_environment_filtered(self):
        self.edit("VALUE = 1 # edit\n")
        key = "sk" + "-" + "A1b2C3d4" * 6
        code = ("import os,sys; assert 'OPENAI_API_KEY' not in os.environ; "
                "assert sys.stdin.read() == ''; print('x' * 9000); print(" + repr(key) + "); "
                "print('safe', file=sys.stderr)")
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": key}):
            self.outcome(self.call(commands=[gl.Command("log", [sys.executable, "-c", code])]), "accepted")
        output = (self.run_dir / "gate/log.log").read_bytes()
        self.assertLessEqual(len(output), 4096)
        self.assertNotIn(key.encode(), output)

    def test_multiline_logs_redact_before_tail_and_oversize_is_not_seen(self):
        self.edit("VALUE = 1 # edit\n")
        block = "-----BEGIN PRIVATE KEY-----\n" + "YWJjZGVmZ2hpamts\n" * 5 + "-----END PRIVATE KEY-----\n"
        command = gl.Command("multiline", [sys.executable, "-c", "print(" + repr(block) + ")"])
        self.outcome(self.call(commands=[command]), "accepted")
        output = (self.run_dir / "gate/multiline.log").read_text()
        self.assertNotIn("YWJjZGVmZ2hpamts", output)
        command = gl.Command("large", [sys.executable, "-c", "print('x' * (1024 * 1024 + 1))"])
        self.outcome(self.call(commands=[command]), "unverified", "project_checks", "not_seen")
        self.assertEqual((self.run_dir / "gate/large.log").read_text(),
                         "not_seen: log_observation_incomplete\n")

    def test_raw_snapshot_does_not_execute_clean_filters(self):
        self.write(self.path, ".gitattributes", "value.py filter=gate-probe diff\n")
        self.git_run(self.path, "add", ".gitattributes")
        self.git_run(self.path, "commit", "--quiet", "-m", "trusted attributes")
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, "rev-parse", "HEAD").strip())
        script = self.directory / "filter.py"
        script.write_text("from pathlib import Path\nPath(" + repr(str(self.marker)) + ").touch()\n")
        self.git_run(self.path, "config", "filter.gate-probe.clean", sys.executable + " " + str(script))
        self.addCleanup(self.git_run, self.path, "config", "--unset", "filter.gate-probe.clean")
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        self.edit("VALUE = 1 # edit\n")
        self.outcome(self.call(), "accepted")
        self.assertFalse(self.marker.exists())
        (self.path / "value.py").write_bytes(b"VALUE = 1 # edit\0\n")
        result = self.call(commands=self.marker_command)
        self.outcome(result, "escalated", "scope")
        self.assertTrue(result["diff"]["files"][0]["binary"])
        self.assertFalse(self.marker.exists())

    def test_dirty_submodule_is_observed_and_escalated(self):
        sub = self.path / "sub"
        sub.mkdir()
        self.addCleanup(shutil.rmtree, sub, True)
        self.git_run(sub, "init", "--quiet", "--template=")
        self.git_run(sub, "config", "user.name", "Fixture")
        self.git_run(sub, "config", "user.email", "fixture@example.invalid")
        self.git_run(sub, "config", "commit.gpgsign", "false")
        self.write(sub, "inside.txt", "before\n")
        self.write(sub, ".gitattributes", "inside.txt filter=gate-probe\n")
        self.git_run(sub, "add", "inside.txt", ".gitattributes")
        self.git_run(sub, "commit", "--quiet", "-m", "nested seed")
        head = self.git_run(sub, "rev-parse", "HEAD").strip()
        self.git_run(self.path, "update-index", "--add", "--cacheinfo", "160000," + head + ",sub")
        self.git_run(self.path, "commit", "--quiet", "-m", "gitlink")
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, "rev-parse", "HEAD").strip())
        self.classification = Classification("write", "fast", ("sub",), ())
        script = self.directory / "sub-filter.py"
        script.write_text("from pathlib import Path\nPath(" + repr(str(self.marker)) + ").touch()\n")
        self.git_run(sub, "config", "filter.gate-probe.clean", sys.executable + " " + str(script))
        self.write(sub, "inside.txt", "after\n")
        result = self.call(commands=self.marker_command)
        self.outcome(result, "escalated", "scope")
        self.assertTrue(result["diff"]["files"][0]["submodule"])
        self.assertFalse(self.marker.exists())

    def test_accepted_target_blob_binds_verified_raw_bytes(self):
        content = "VALUE = 1 # frozen edit\n"
        self.edit(content)
        result = self.call(commands=self.marker_command)
        self.outcome(result, "accepted")
        diff = result["diff"]
        self.assertEqual(diff["target_path"], "value.py")
        self.assertEqual(diff["target_blob"], hashlib.sha1(
            b"blob " + str(len(content.encode())).encode() + b"\0" + content.encode()).hexdigest())
        self.edit("VALUE = 99\n")
        self.assertEqual(self.git_run(self.path, "cat-file", "blob", diff["target_blob"]), content)
        for key, values in (("target_blob", (None, "x" * 40, "a" * 39, "a" * 41, 1)),
                            ("target_path", (None, "../bad", "/bad", "other.py", 1))):
            missing = deepcopy(result)
            del missing["diff"][key]
            with self.assertRaises(ValueError):
                gl.validate_gate_result(missing)
            for value in values:
                changed = deepcopy(result)
                changed["diff"][key] = value
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    gl.validate_gate_result(changed)
        for length in (40, 64):
            changed = deepcopy(result)
            changed["diff"]["target_blob"] = "a" * length
            gl.validate_gate_result(changed)

    def test_unaccepted_result_does_not_require_frozen_target(self):
        result = self.call(commands=self.marker_command)
        self.assertNotEqual(result["verdict"], "accepted")
        self.assertNotIn("target_blob", result["diff"])
        self.assertNotIn("target_path", result["diff"])
        gl.validate_gate_result(result)

    def test_validator_mutations(self):
        self.edit("VALUE = 1 # edit\n")
        valid = self.call(commands=self.marker_command)
        self.outcome(valid, "accepted")
        mutations = []
        for key in valid:
            missing = deepcopy(valid)
            del missing[key]
            mutations.append(missing)
        mutations.extend(dict(deepcopy(valid), **change) for change in (
            {"extra": 0}, {"verdict": "unknown"}, {"checks": []}, {"escalation": "lane"},
            {"warnings": [1]}, {"warnings": ["empty_diff"]}, {"diff": {}}))
        for status in ("fail", "not_seen", "skipped", "unknown"):
            for index in range(len(gl._IDS)):
                changed = deepcopy(valid)
                changed["checks"][index]["status"] = status
                mutations.append(changed)
        for change in ({"detail": "x" * 301}, {"duration_ms": True}, {"extra": 0}, {"id": "unknown"}):
            changed = deepcopy(valid)
            changed["checks"][0].update(change)
            mutations.append(changed)
        for changed in mutations:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                gl.validate_gate_result(changed)

    def test_fingerprint_covers_all_controls_and_hardens_git(self):
        before = self.controls_before
        with mock.patch.object(gl, '_git_bytes', side_effect=AssertionError('Git must not run')):
            self.assertEqual(gl.snapshot_controls(self.main, self.wt), before)
        common = self.main / '.git'
        _, _, registration = gl._control_locations(self.main, self.wt)
        paths = [common / n for n in ('config', 'config.worktree', 'info/attributes',
                 'info/exclude', 'info/sparse-checkout', 'info/grafts', 'info/extra/nested',
                 'shallow', 'refs/replace/nested/ref', 'objects/info/alternates', 'hooks/sub/hook')]
        paths += [registration / n for n in ('config.worktree', 'commondir', 'gitdir',
                  'info/attributes', 'info/exclude', 'info/sparse-checkout', 'info/grafts',
                  'info/extra/nested', 'shallow', 'objects/info/alternates')]
        paths += [self.path / '.git']
        for file in paths:
            old = file.read_bytes() if file.exists() else None
            file.parent.mkdir(parents=True, exist_ok=True)
            try:
                file.write_bytes((old or b'') + b'changed')
                self.assertNotEqual(gl.snapshot_controls(self.main, self.wt), before, str(file))
            finally:
                if old is None:
                    file.unlink()
                else:
                    file.write_bytes(old)
                if file.parent.name == 'sub':
                    file.parent.rmdir()
        args, env = gl._git_setup()
        for setting in ('core.attributesFile=' + os.devnull, 'protocol.allow=never',
                        'core.fsmonitor=false', 'core.hooksPath=' + os.devnull):
            self.assertIn(setting, args)
        self.assertEqual(env['GIT_NO_LAZY_FETCH'], '1')
        self.assertEqual(env['GIT_ATTR_NOSYSTEM'], '1')
        self.assertEqual(env['GIT_NO_REPLACE_OBJECTS'], '1')
        self.assertEqual(env['GIT_GRAFT_FILE'], os.devnull)
        self.assertIn('--no-replace-objects', args)
        with mock.patch.dict(os.environ, {'GIT_NO_REPLACE_OBJECTS': '0',
                             'GIT_GRAFT_FILE': '/evil/grafts', 'GIT_SHALLOW_FILE': '/evil/shallow'}):
            _, env = gl._git_setup()
        self.assertEqual(env['GIT_NO_REPLACE_OBJECTS'], '1')
        self.assertEqual(env['GIT_GRAFT_FILE'], os.devnull)
        self.assertNotIn('GIT_SHALLOW_FILE', env)

    def _plant_blob_replacement(self, packed=False):
        self.edit("VALUE = 1 # edit\n")
        evil = self.directory / 'evil-blob'
        evil.write_text('from pathlib import Path\nPath(%r).touch()\n' % str(self.marker))
        oid = self.git_run(self.main, 'hash-object', '-w', str(evil)).strip()
        original = self.git_run(self.main, 'rev-parse',
                                self.wt.base_commit + ':tests/__init__.py').strip()
        if packed:
            refs = self.main / '.git/packed-refs'
            before = refs.read_bytes() if refs.exists() else None
            self.addCleanup(lambda: refs.unlink() if before is None else refs.write_bytes(before))
            refs.write_bytes((before or b'') + ('%s refs/replace/%s\n' % (oid, original)).encode('ascii'))
        else:
            folder = self.main / '.git/refs/replace'
            existed = folder.exists()
            if not existed:
                self.addCleanup(folder.rmdir)
            self.git_run(self.path, 'replace', original, oid)
            self.addCleanup(self.git_run, self.main, 'replace', '-d', original)
        return original

    def test_y1_refs_replace_blob_never_runs_planted_code(self):
        self._plant_blob_replacement()
        with mock.patch.object(gl, '_git_bytes', wraps=gl._git_bytes) as git:
            self.outcome(self.call(), 'escalated', 'controls')
            git.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_y1_packed_refs_replace_blob_never_runs_planted_code(self):
        self._plant_blob_replacement(packed=True)
        with mock.patch.object(gl, '_git_bytes', wraps=gl._git_bytes) as git:
            self.outcome(self.call(), 'escalated', 'controls')
            git.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_packed_replacement_fingerprint_includes_peeled_oid_only_for_replacements(self):
        refs = self.main / '.git/packed-refs'
        old = refs.read_bytes() if refs.exists() else None
        self.addCleanup(lambda: refs.unlink() if old is None else refs.write_bytes(old))
        ordinary = (old or b'') + (self.wt.base_commit + ' refs/heads/ordinary\n').encode('ascii')
        refs.write_bytes(ordinary)
        self.assertEqual(gl.snapshot_controls(self.main, self.wt), self.controls_before)
        entry = ('%s refs/replace/%s\n' % (self.wt.base_commit, self.wt.base_commit)).encode('ascii')
        refs.write_bytes(ordinary + entry + b'^' + b'1' * 40 + b'\n')
        before = gl.snapshot_controls(self.main, self.wt)
        self.assertNotEqual(before, self.controls_before)
        refs.write_bytes(ordinary + entry + b'^' + b'2' * 40 + b'\n')
        self.assertNotEqual(gl.snapshot_controls(self.main, self.wt), before)

    def test_y1_no_replace_objects_positive_control(self):
        original = self._plant_blob_replacement()
        # Model a replacement already present before the executor. Even when
        # controls match, every Git tree/blob read must ignore its content.
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        self.assertIn('Path(', self.git_run(self.main, 'show', original))
        self.assertEqual(gl._git_bytes(self.main, 'show', original)[1], b'')
        with mock.patch.dict(os.environ, {'GIT_SHALLOW_FILE': '/evil/shallow'}):
            self.outcome(self.call(), 'accepted')
        self.assertFalse(self.marker.exists())

    def test_y1_poisoned_blobs_fail_independent_checkout_verification(self):
        self._plant_blob_replacement()
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        safe_git = gl._git_bytes

        def unsafe_blobs(path, *args, **kwargs):
            if args[0] == 'cat-file' and '--batch' in args:
                # Deliberately bypass only blob-reading Git hardening. The real
                # replacement-poisoned bytes must still fail before execution.
                data = subprocess.run([self.git, *args], cwd=str(path), env=self.env,
                                      input=kwargs.get('input_data'), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                      check=True, timeout=scaled(5)).stdout
                return 0, data
            return safe_git(path, *args, **kwargs)

        with mock.patch.object(gl, '_git_bytes', side_effect=unsafe_blobs), \
                mock.patch.object(gl, '_project', wraps=gl._project) as project:
            self.outcome(self.call(), 'unverified', 'project_checks', 'not_seen')
            project.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_y1_grafts_change_stops_before_git(self):
        self.edit("VALUE = 1 # edit\n")
        grafts = self.main / '.git/info/grafts'
        before = grafts.read_bytes() if grafts.exists() else None
        grafts.parent.mkdir(exist_ok=True)
        self.addCleanup(lambda: grafts.unlink() if before is None else grafts.write_bytes(before))
        grafts.write_text(self.wt.base_commit + '\n')
        with mock.patch.object(gl, '_git_bytes', wraps=gl._git_bytes) as git:
            self.outcome(self.call(commands=self.marker_command), 'escalated', 'controls')
            git.assert_not_called()
        self.assertFalse(self.marker.exists())

    def test_checkout_ignores_archive_attributes_and_preserves_raw_bytes(self):
        # Attributes must never transform the materialised raw objects.
        for attribute in ('export-subst', 'export-ignore'):
            with self.subTest(attribute=attribute):
                self.write(self.path, 'tests/__init__.py', '# $Format:%H$\n')
                self.write(self.path, '.gitattributes', 'tests/__init__.py ' + attribute + '\n')
                self.git_run(self.path, 'add', 'tests/__init__.py', '.gitattributes')
                self.git_run(self.path, 'commit', '-qm', 'trusted archive attributes')
                self.wt = replace(self.wt, base_commit=self.git_run(self.path, 'rev-parse', 'HEAD').strip())
                self.controls_before = gl.snapshot_controls(self.main, self.wt)
                self.edit("VALUE = 1 # edit\n")
                with mock.patch.object(gl, '_project', wraps=gl._project) as project:
                    self.outcome(self.call(commands=self.marker_command), 'accepted')
                    project.assert_called_once()
                self.assertIn('command-ran', (self.run_dir / 'gate/marker.log').read_text())

    def test_checkout_verifies_quoted_paths_and_symlink_blob_bytes_in_one_batch(self):
        names = ['line\nbreak.txt', 'tab\tquote"\\雪.txt', 'safe-link']
        for name in names[:-1]:
            self.write(self.path, name, 'base bytes\n')
        (self.path / names[-1]).symlink_to(names[0])
        self.git_run(self.path, 'add', '--', *names)
        self.git_run(self.path, 'commit', '-qm', 'trusted unusual paths')
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, 'rev-parse', 'HEAD').strip())
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        self.edit("VALUE = 1 # edit\n")
        with mock.patch.object(gl, '_git_bytes', wraps=gl._git_bytes) as git:
            self.outcome(self.call(), 'accepted')
        batches = [c for c in git.call_args_list if '--stdin-paths' in c.args]
        self.assertEqual(len(batches), 1)
        self.assertIn('--no-filters', batches[0].args)
        self.assertFalse(self.marker.exists())

    def test_y2_php_attribute_change_is_unverifiable_before_make(self):
        self.write(self.path, 'app.php', '<?php\n#[Route("/a")]\nfunction f(){}\n')
        self.write(self.path, 'Makefile', 'test:\n\t@true\n')
        self.git_run(self.path, 'add', 'app.php', 'Makefile')
        self.git_run(self.path, 'commit', '-qm', 'trusted php and make')
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, 'rev-parse', 'HEAD').strip())
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        self.classification = Classification('write', 'fast', ('app.php',), ())
        self.edit('<?php\n#[Route("/EVIL")]\nfunction f(){}\n', 'app.php')
        command = gl.Command('make-test', ['make', 'test'], 'Makefile')
        with mock.patch.object(gl, '_project', wraps=gl._project) as project:
            result = self.call(commands=[command])
            self.outcome(result, 'escalated', 'code_unchanged')
            project.assert_not_called()
        self.assertEqual(result['checks'][6]['detail'], 'code_change_unverifiable')
        self.assertFalse(self.marker.exists())

    def test_php_ruby_unsupported_and_hash_syntax_preserved_in_c_like_sources(self):
        for name, before, after in (
                ('a.php', '$x=1; #[Route("/a")]', '$x=1; #[Route("/evil")]'),
                ('a.php', 'class C { #[Pure] function f(){} }',
                 'class C { #[Deprecated] function f(){} }'),
                ('a.rb', 'x = 1 # before', 'x = 1 # after')):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'unsupported_source'):
                gl._same_code(name, before, after)
        for suffix in ('.c', '.h', '.cc', '.cpp', '.hpp', '.cs', '.swift', '.rs'):
            for before, after in (('#define X 1\n', '#define X 2\n'),
                                  ('#[safe]\nfn f() {}', '#[evil]\nfn f() {}'),
                                  ('#![safe]\nfn f() {}', '#![evil]\nfn f() {}')):
                with self.subTest(suffix=suffix, before=before):
                    self.assertFalse(gl._same_code('a' + suffix, before, after))
                    self.assertTrue(gl._same_code('a' + suffix, before + ' // before',
                                                 before + ' // after'))

    def test_rust_attribute_change_escalates(self):
        self.write(self.path, 'value.rs', '#[inline]\nfn f() {}\n')
        self.git_run(self.path, 'add', 'value.rs')
        self.git_run(self.path, 'commit', '-qm', 'trusted rust')
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, 'rev-parse', 'HEAD').strip())
        self.classification = Classification('write', 'fast', ('value.rs',), ())
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        for text in ('#[cold]\nfn f() {}\n', '#![no_std]\nfn f() {}\n'):
            with self.subTest(text=text):
                self.edit(text, 'value.rs')
                self.outcome(self.call(commands=self.marker_command), 'escalated', 'code_unchanged')
                self.assertFalse(self.marker.exists())

    def test_bad_inputs_and_transient_receipt_error_return_fresh_valid_results(self):
        self.edit("VALUE = 1 # edit\n")
        for args in ({'total_timeout_s': float('nan')}, {'diff_line_limit': None},
                     {'added_byte_limit': -1}, {'controls_before': {}}, {'commands': [None]},
                     {'retry': 1}):
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / 'gate.json').write_text('{"stale":true}')
            result = self.call(**args)
            self.assertNotEqual(result['verdict'], 'accepted')
            gl.validate_gate_result(result)
            self.assertEqual(json.loads((self.run_dir / 'gate.json').read_text()), result)
        write = gl.safe_write_json
        count = [0]
        def once(*args, **kwargs):
            count[0] += 1
            if count[0] == 1:
                raise OSError('do-not-log')
            return write(*args, **kwargs)
        with mock.patch.object(gl, 'safe_write_json', side_effect=once):
            self.outcome(self.call(), 'unverified', 'project_checks', 'not_seen')
        self.assertEqual(count[0], 2)

    def test_controls_missing_and_mutation_prevent_all_git(self):
        self.edit("VALUE = 1 # edit\n")
        with mock.patch.object(gl, '_git_bytes', wraps=gl._git_bytes) as git:
            self.outcome(self.call(controls_before=None), 'unverified', 'controls', 'not_seen')
            git.assert_not_called()
            attr = self.main / '.git/info/attributes'
            attr.write_text('value.py -whitespace\n')
            self.addCleanup(attr.unlink)
            self.outcome(self.call(), 'escalated', 'controls')
            git.assert_not_called()

    def test_f1_poisoned_pyc_and_f9_test_neutralisation(self):
        self.edit('VALUE = -5\n')
        source = self.directory / 'evil.py'
        source.write_text('from pathlib import Path\nPath(%r).touch()\n' % str(self.marker))
        with mock.patch.object(sys, 'pycache_prefix', None):
            cache = Path(importlib.util.cache_from_source(str(self.path / 'tests/test_value.py')))
        cache.parent.mkdir(exist_ok=True)
        py_compile.compile(str(source), cfile=str(cache),
                           invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
        self.outcome(self.call(), 'escalated', 'code_unchanged')
        self.assertFalse(self.marker.exists())
        self.edit('VALUE = 1\n')
        self.classification = Classification('write', 'fast', ('tests/test_value.py',), ())
        original = (self.path / 'tests/test_value.py').read_text()
        for injection in ("        self.skipTest('x')\n", '        return\n',
                          "        raise unittest.SkipTest('x')\n", 'import os; os._exit(0)\n'):
            content = (injection + original if injection.startswith('import') else
                       original.replace('        self.assert', injection + '        self.assert'))
            self.edit(content, 'tests/test_value.py')
            self.outcome(self.call(commands=self.marker_command), 'escalated', 'code_unchanged')
            self.assertFalse(self.marker.exists())

    def test_f2_promisor_uploadpack_and_includeif_never_start_git(self):
        common = self.main / '.git'
        config = common / 'config'
        original = config.read_bytes()
        registration = next((common / 'worktrees').iterdir())
        scoped = registration / 'config.worktree'
        include = self.directory / 'include.cfg'
        evil = ('[extensions]\n partialClone = evil\n[remote "evil"]\n'
                ' url = /nonexistent\n promisor = true\n uploadpack = "touch %s; false"\n' % self.marker)
        include.write_text(evil)
        self.edit("VALUE = 1 # edit\n")
        try:
            for variant in ('common', 'worktree-includeif'):
                config.write_bytes(original)
                if variant == 'common':
                    config.write_bytes(original + evil.encode())
                else:
                    config.write_bytes(original + b'\n[extensions]\n worktreeConfig = true\n')
                    scoped.write_text('[includeIf "gitdir:**"]\n path = %s\n' % include)
                # A missing staged object would trigger lazy fetch in the old gate.
                subprocess.run([self.git, 'update-index', '--cacheinfo',
                                '100644,' + 'ab' * 20 + ',value.py'], cwd=str(self.path),
                               env=dict(self.env, GIT_NO_LAZY_FETCH='1'), check=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                with mock.patch.object(gl, '_git_bytes', wraps=gl._git_bytes) as git:
                    self.outcome(self.call(), 'escalated', 'controls')
                    git.assert_not_called()
                self.assertFalse(self.marker.exists())
        finally:
            config.write_bytes(original)
            if scoped.exists():
                scoped.unlink()

    def test_f3_late_writer_cannot_change_checked_bytes_or_checkout(self):
        self.edit("VALUE = 1 # edit\n")
        ready = self.directory / 'snapshot-ready'
        done = self.directory / 'writer-done'
        code = ("import pathlib,time,sys\nready,done,wt,marker=map(pathlib.Path,sys.argv[1:])\n"
                "while not ready.exists(): time.sleep(.001)\n"
                "(wt/'value.py').write_text('VALUE = -5\\n')\n"
                "(wt/'tests/test_zz_late.py').write_text('from pathlib import Path\\nPath(%r).touch()\\n' % str(marker))\n"
                "done.touch()\n")
        child = subprocess.Popen([sys.executable, '-c', code, str(ready), str(done),
                                  str(self.path), str(self.marker)], env=self.env)
        self.addCleanup(lambda: child.poll() is None and child.kill())
        checkout = gl._checkout
        def after_snapshot(*args):
            ready.touch()
            child.wait(timeout=scaled(3))
            self.assertTrue(done.exists())
            return checkout(*args)
        with mock.patch.object(gl, '_checkout', side_effect=after_snapshot):
            self.outcome(self.call(), 'accepted')
        self.assertFalse(self.marker.exists())
        self.assertFalse((self.path / '__pycache__').exists())
        # The next gate sees the late executor output and refuses it.
        self.outcome(self.call(), 'escalated', 'scope')

    def test_f5_attributes_cannot_disable_python_whitespace_check(self):
        attr = self.main / '.git/info/attributes'
        attr.write_text('value.py -whitespace conflict-marker-size=99\n')
        self.addCleanup(attr.unlink)
        self.controls_before = gl.snapshot_controls(self.main, self.wt)
        self.edit('VALUE = 1 # edit \t \n<<<<<<< ours\n')
        self.outcome(self.call(commands=self.marker_command), 'needs_fix', 'diff_check')
        self.assertFalse(self.marker.exists())

    def test_f6_multiline_secret_in_worktree_index_and_intermediate_commit(self):
        original = 'VALUE = 1\n'
        secret = '"""\napi_key:\n  9f8e7d6c5b4a39281706f5e4d3c2b1a0\n"""\n' + original
        self.assertNotEqual(gl.redact_text(secret), secret)
        for state in ('worktree', 'index', 'history'):
            self.git_run(self.path, 'reset', '--hard', self.wt.base_commit)
            self.edit(secret)
            if state != 'worktree':
                self.git_run(self.path, 'add', 'value.py')
                if state == 'history':
                    self.git_run(self.path, 'commit', '-qm', 'secret')
                    self.edit(original)
                    self.git_run(self.path, 'commit', '-qam', 'restore')
                self.edit("VALUE = 1 # edit\n")
            self.outcome(self.call(commands=self.marker_command), 'needs_fix', 'secrets')
            self.assertFalse(self.marker.exists())
            self.assertNotIn('9f8e7d6c5b4a39281706f5e4d3c2b1a0', (self.run_dir / 'gate.json').read_text())

    def test_f7_odd_path_unmerged_and_unexpected_exception_replace_stale_receipt(self):
        for variant in ('odd', 'unmerged', 'exception'):
            self.git_run(self.path, 'reset', '--hard', self.wt.base_commit)
            self.edit("VALUE = 1 # edit\n")
            self.run_dir.mkdir(parents=True, exist_ok=True)
            (self.run_dir / 'gate.json').write_text('{"stale":"accepted"}')
            if variant == 'odd':
                self.edit('x\n', 'C:x')
                self.git_run(self.path, 'add', 'C:x')
                self.git_run(self.path, 'commit', '-qm', 'odd')
                self.git_run(self.path, 'rm', '-q', 'C:x')
            elif variant == 'unmerged':
                blob = self.git_run(self.path, 'hash-object', '-w', 'value.py').strip()
                data = ('0 ' + '0' * 40 + '\tvalue.py\n' + ''.join(
                    '100644 %s %d\tvalue.py\n' % (blob, stage) for stage in (1, 2, 3)))
                subprocess.run([self.git, 'update-index', '--index-info'], cwd=str(self.path),
                               env=self.env, input=data.encode(), check=True)
            if variant == 'exception':
                with mock.patch.object(gl, '_collect', side_effect=RuntimeError('secret-do-not-log')):
                    result = self.call(commands=self.marker_command)
            else:
                result = self.call(commands=self.marker_command)
            self.outcome(result, 'unverified', 'diff_collect', 'not_seen')
            self.assertNotIn('secret-do-not-log', (self.run_dir / 'gate.json').read_text())
            self.assertFalse(self.marker.exists())

    def test_f8_detached_head_and_reverted_outside_commit(self):
        self.edit('OTHER = 999\n', 'other.py')
        self.git_run(self.path, 'commit', '-qam', 'outside')
        self.git_run(self.path, 'checkout', '-q', '--detach', self.wt.base_commit)
        self.edit("VALUE = 1 # edit\n")
        try:
            self.outcome(self.call(commands=self.marker_command), 'escalated', 'scope')
        finally:
            self.git_run(self.path, 'checkout', '-qf', self.wt.branch)
        self.edit('OTHER = 1\n', 'other.py')
        self.git_run(self.path, 'commit', '-qam', 'restore outside')
        self.edit("VALUE = 1 # edit\n")
        self.outcome(self.call(commands=self.marker_command), 'escalated', 'scope')
        self.assertFalse(self.marker.exists())

    def test_positive_docstring_typescript_comment_and_readme_use_private_checkout(self):
        self.write(self.path, 'tests/test_value.py',
                   "import os,unittest\nfrom pathlib import Path\nclass Tests(unittest.TestCase):\n"
                   "    def test_cwd(self):\n        Path('observed-cwd').write_text(os.getcwd())\n")
        self.write(self.path, 'notes.ts', 'const value = "/* string */"; // before\n')
        self.write(self.path, 'README.md', 'A tpyo.\n')
        self.git_run(self.path, 'add', 'notes.ts', 'README.md', 'tests/test_value.py')
        self.git_run(self.path, 'commit', '-qm', 'trusted targets')
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, 'rev-parse', 'HEAD').strip())
        for name, text in (('value.py', '"""A new docstring."""\nVALUE = 1\n'),
                           ('notes.ts', 'const value = "/* string */"; // after\n'),
                           ('README.md', 'A typo.\n')):
            self.git_run(self.path, 'reset', '--hard', self.wt.base_commit)
            self.classification = Classification('write', 'fast', (name,), ())
            self.edit(text, name)
            with mock.patch.dict(os.environ, {'ORCEXE_KEEP_GATE_DIR': '1'}):
                self.outcome(self.call(), 'accepted')
            cache = self.home / '.cache/orcexe/gate'
            roots = list(cache.iterdir())
            self.assertEqual(len(roots), 1)
            root = roots[0]
            self.assertEqual(root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((root / 'checkout' / name).read_text(), text)
            self.assertEqual((root / 'checkout/observed-cwd').read_text(), str(root / 'checkout'))
            self.assertFalse((self.path / 'tests/__pycache__').exists())
            self.assertIn('Ran 1 test', (self.run_dir / 'gate/unittest.log').read_text())
            shutil.rmtree(root)

    def test_byte_budget_and_unsupported_source_constructs_fail_closed(self):
        self.edit('VALUE = 1\n# ' + 'a' * 20001 + '\n')
        self.outcome(self.call(commands=self.marker_command), 'escalated', 'size')
        self.assertFalse(self.marker.exists())
        for name, text in (('a.ts', 'const a = `x`;'), ('a.rs', 'let a = r#"x"#;'),
                           ('a.rs', '/* outer /* inner */ */'), ('a.go', 'var a = `x`'),
                           ('a.kt', 'val a = """x"""'), ('a.swift', 'let a = """x"""'),
                           ('a.rb', 'a = <<END\nx\nEND'), ('a.php', 'a = <<<END\nx\nEND')):
            with self.subTest(name=name, text=text), self.assertRaises(ValueError):
                gl._same_code(name, text, text + '\n// comment')
        self.edit('VALUE = (\n')
        self.outcome(self.call(commands=self.marker_command), 'escalated', 'code_unchanged')

    def test_gate_environment_and_cleanup(self):
        self.edit("VALUE = 1 # edit\n")
        code = 'import os,json; print(json.dumps(dict(env=dict(os.environ),cwd=os.getcwd())))'
        self.outcome(self.call(commands=[gl.Command('env', [sys.executable, '-c', code])]), 'accepted')
        data = json.loads((self.run_dir / 'gate/env.log').read_text())
        root = Path(data['cwd']).parent
        self.assertTrue(str(root).startswith(str(self.home / '.cache/orcexe/gate')))
        temporary = Path(data['env']['TMPDIR'])
        for key in ('HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'CODEX_HOME'):
            self.assertEqual(Path(data['env'][key]).parent, temporary)
        self.assertEqual(data['env']['TEMP'], str(temporary))
        self.assertEqual(data['env']['TMP'], str(temporary))
        self.assertFalse(root.exists())
        self.assertFalse(temporary.exists())

    def test_f4_pytest_ancestor_code_never_runs(self):
        if importlib.util.find_spec('pytest') is None:
            self.skipTest('pytest not installed for ' + sys.executable)
        self._parent_attack('pytest')

    def test_f4_npm_ancestor_binary_never_runs(self):
        if shutil.which('npm', path=self.env['PATH']) is None:
            self.skipTest('npm not installed')
        self._parent_attack('npm')

    def _parent_attack(self, tool):
        self.git_run(self.path, 'reset', '--hard', self.wt.base_commit)
        self.git_run(self.path, 'clean', '-fdx')
        if tool == 'pytest':
            self.write(self.path, 'conftest.py', '')
            self.write(self.path, 'tests/test_value.py', 'def test_value():\n    assert False\n')
            self.write(self.path.parent, 'pytest.ini', '[pytest]\n')
            self.write(self.path.parent, 'conftest.py',
                       'from pathlib import Path\nPath(%r).touch()\n' % str(self.marker))
            command = gl.Command('pytest', [sys.executable, '-m', 'pytest', '-q'])
        else:
            self.write(self.path, 'package.json', '{"scripts":{"test":"l2tool"}}')
            planted = self.path.parent / 'node_modules/.bin/l2tool'
            self.write(self.path.parent, 'node_modules/.bin/l2tool',
                       '#!/bin/sh\ntouch %s\nexit 0\n' % self.marker)
            planted.chmod(0o755)
            command = gl.Command('npm', ['npm', '--offline', '--no-update-notifier', 'run', 'test'])
        self.git_run(self.path, 'add', '.')
        self.git_run(self.path, 'commit', '-qm', 'trusted command definitions')
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, 'rev-parse', 'HEAD').strip())
        self.edit("VALUE = 1 # edit\n")
        self.outcome(self.call(commands=[command]), 'needs_fix', 'project_checks')
        self.assertFalse(self.marker.exists())
        for file in (self.path.parent / 'pytest.ini', self.path.parent / 'conftest.py'):
            if file.exists():
                file.unlink()
        shutil.rmtree(self.path.parent / 'node_modules', ignore_errors=True)



class DetectionTests(GitFixture):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        fixture = cls.shared = GitFixture()
        fixture.templates = cls.templates
        fixture.setUp()
        _native_git(fixture)
        cls.addClassCleanup(fixture.doCleanups)
        fixture.git_run(fixture.main, 'commit', '-qm', 'empty seed', '--allow-empty')
        cls.base = fixture.git_run(fixture.main, 'rev-parse', 'HEAD').strip()

    def setUp(self):
        self.copy_fixture(self.shared)
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)

    def define(self, files):
        for name, text in files.items():
            GateTests.write(self.main, name, text)
        self.git_run(self.main, "add", ".")
        self.git_run(self.main, "commit", "--quiet", "--allow-empty", "-m", "definitions")
        return self.git_run(self.main, "rev-parse", "HEAD").strip()

    def test_all_detection_rules_in_order_from_base(self):
        base = self.define({"package.json": json.dumps({"scripts": dict(test="x", lint="x", build="x")}),
             "pnpm-lock.yaml": "", "yarn.lock": "", "bun.lockb": "", "Cargo.toml": "",
             "pyproject.toml": "[tool.pytest.ini_options]\n", "go.mod": "module example\n",
             "Makefile": "test:\nlint:\ncheck:\nnot-test:\n"})
        GateTests.write(self.main, "package.json", "invalid changed definition")
        commands = gl.detect_commands(self.main, base)
        self.assertEqual([c.argv for c in commands], [["pnpm", "run", n] for n in ("test", "lint", "build")]
             + [["python3", "-m", "pytest", "-q"]] + [["make", n] for n in ("test", "lint", "check")]
             + [["cargo", "test"], ["go", "test", "./..."]])

    def test_manager_lock_precedence(self):
        self.define({"package.json": '{"scripts":{"test":"x"}}'})
        for lock, manager in ((None, "npm"), ("bun.lockb", "bun"), ("yarn.lock", "yarn"), ("pnpm-lock.yaml", "pnpm")):
            with self.subTest(manager=manager):
                base = self.define({lock: ""} if lock else {})
                self.assertEqual(gl.detect_commands(self.main, base)[0].argv, [manager, "run", "test"])

    def test_pytest_sources_and_unittest_fallback_and_none(self):
        base = self.define({"value.py": "VALUE = 1\n"})
        self.assertEqual(gl.detect_commands(self.main, base), [])
        wt = worktree.create(self.main, worktree.Spec("plan", "no-commands", "fake"))
        GateTests.write(Path(wt.path), "value.py", "VALUE = 1 # edit\n")
        run_dir = self.main / ".orcexe/runs/no-commands"
        result = gl.gate(repo_path=self.main, worktree=wt,
                         classification=Classification("write", "fast", ("value.py",), ()),
                         controls_before=gl.snapshot_controls(self.main, wt),
                         run_result=valid_result(run_dir), run_dir=run_dir,
                         command_timeout_s=scaled(5), total_timeout_s=scaled(15))
        self.assertEqual(result["verdict"], "unverified")
        self.assertEqual(result["checks"][-1]["status"], "not_seen")
        base = self.define({"tests/test_a.py": "", "tests/__init__.py": ""})
        self.assertEqual(gl.detect_commands(self.main, base)[0].id, "unittest")
        for name, text in (("conftest.py", ""), ("setup.cfg", "[tool:pytest]\n"),
                           ("pytest.ini", "[pytest]\n"), ("pyproject.toml", "[tool.pytest.ini_options]\n")):
            with self.subTest(name=name):
                base = self.define({name: text})
                self.assertEqual(gl.detect_commands(self.main, base)[0].source_path, name)

    def test_main_override_wins_from_linked_worktree(self):
        base = self.define({"Makefile": "test:\n"})
        linked = self.linked()
        GateTests.write(self.main, ".orcexe/commands.yaml",
                        "version: 1\ncommands: [{id: explicit, argv: [python3, -V]}]\n")
        self.assertEqual(gl.detect_commands(linked, base),
                         [gl.Command("explicit", ["python3", "-V"], ".orcexe/commands.yaml")])

    def test_static_test_floor_language_table(self):
        for name, text, tests, skips in (
            ("a.py", "def test_one(): pass\nasync def test_two(): pass\n@unittest.skip('x')\n"
             "@pytest.mark.skipif(True)\npytest.skip('x')\nxfail\n", 2, 4),
            ("a.ts", "it('x'); test('x'); describe('x'); it.skip('x'); test.only('x'); xit('x'); xdescribe('x');", 3, 4),
            ("a.go", "func TestOne(t *testing.T) {}\nt.Skip('x')", 1, 1),
            ("a.rs", "#[test]\nfn a() {}\n#[ignore]", 1, 1),
            ("a.java", "@Test\n@Disabled", 1, 1), ("a.kt", "@Test\n@Disabled", 1, 1)):
            with self.subTest(name=name):
                self.assertEqual(gl._counts(name, text), (tests, skips))
