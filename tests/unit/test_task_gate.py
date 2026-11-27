"""Standard-gate test floor over controlled raw blob observations."""

import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.unit.test_run_result import valid_result
from orcexe import gate_lite, task_gate


class TaskGateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orcexe-task-floor-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.name = "tests/unit/test_x.py"

    def call(self, before, after):
        old, new = "a" * 40, "b" * 40
        file = dict(path=self.name, old_path=None, binary=False, symlink=False,
                    submodule=False, old_mode="100644", new_mode="100644")
        observed = dict(tree="candidate-tree", files=[file], versions=[(file, (old, new))])
        worktree = SimpleNamespace(path=str(self.directory), base_commit="c" * 40)
        # Git observation is a separate boundary; exercise the real per-blob checks.
        with mock.patch.object(gate_lite, "snapshot_controls", return_value={}), \
                mock.patch.object(gate_lite, "_git", return_value=(0, "base-tree")), \
                mock.patch.object(gate_lite, "_blobs", return_value={
                    old: before.encode("utf-8"), new: after.encode("utf-8")}):
            result = task_gate.standard_gate(
                repo_path=self.directory, worktree=worktree,
                run_result=valid_result(self.directory), run_dir=self.directory,
                controls_before={}, commands=[], observed=observed)
        self.assertEqual(json.loads((self.directory / "gate.json").read_text()), result)
        return result

    def test_test_floor_details_include_file_and_each_count_change(self):
        before = "def test_one(): pass\n"
        decorated = "@" + "unittest." + "skip('reason')\n" + before
        for after, change in ((decorated, "skips 0->1"),
                              ("def helper(): pass\n", "tests 1->0"),
                              (decorated.replace("def test_one", "def helper"),
                               "tests 1->0 skips 0->1")):
            with self.subTest(change=change):
                result = self.call(before, after)
                self.assertEqual(result["verdict"], "failed")
                self.assertEqual(result["checks"][-1], dict(
                    id="test_floor", status="fail",
                    detail="test_floor: " + self.name + " " + change))

    def test_python_marker_text_passes_floor_but_real_markers_fail(self):
        before = "def test_one(): pass\n"
        for marker in ("@" + "unittest." + "skip('reason')",
                       "@" + "pytest.mark." + "xf" + "ail",
                       "self." + "skipTest('reason')", "pytest." + "skip('reason')"):
            with self.subTest(marker=marker):
                text_only = ('"""' + marker + '"""\nvalue = ' + json.dumps(marker)
                             + "\n# " + marker + "\n" + before)
                result = self.call(before, text_only)
                floor = next(c for c in result["checks"] if c["id"] == "test_floor")
                self.assertEqual(floor["status"], "pass")
                self.assertEqual(result["checks"][-1]["id"], "project_commands")
                source = marker + "\n" + before if marker.startswith("@") else before + marker + "\n"
                result = self.call(before, source)
                self.assertEqual(result["checks"][-1], dict(
                    id="test_floor", status="fail",
                    detail="test_floor: " + self.name + " skips 0->1"))
