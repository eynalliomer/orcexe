"""Real temporary repositories, including paths unsafe for line-based parsing."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit._timing import scaled

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe import gitutil, proc


class GitFixture(unittest.TestCase):
    """Reusable hermetic Git setup; never writes the source repository's .git."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orcexe-t11-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.main = self.directory / "main"
        self.main.mkdir()
        self.home = self.directory / "home"
        self.home.mkdir()
        self.env = {key: os.environ[key] for key in
                    ("SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC")
                    if key in os.environ}
        self.env.update(PATH=os.pathsep.join((str(Path(sys.executable).parent), os.defpath,
                           os.environ.get("PATH", ""))), HOME=str(self.home),
                        USERPROFILE=str(self.home), CODEX_HOME=str(self.directory / "codex-home"),
                        XDG_CONFIG_HOME=str(self.home / ".config"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
                        GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0")
        self.git = shutil.which("git", path=self.env["PATH"])
        if not self.git:
            self.skipTest("git unavailable")
        self.git_run(self.main, "init", "--quiet", "--template=")
        self.git_run(self.main, "config", "user.name", "Fixture")
        self.git_run(self.main, "config", "user.email", "fixture@example.invalid")
        self.git_run(self.main, "config", "commit.gpgsign", "false")
        self.git_run(self.main, "config", "core.hooksPath", str(self.directory / "no-hooks"))

    def git_run(self, directory, *args):
        return subprocess.run([self.git, *args], cwd=str(directory), env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True, timeout=scaled(5)).stdout.decode("utf-8")

    def seed(self):
        (self.main / "tracked.txt").write_text("before\n", encoding="utf-8")
        (self.main / ".gitignore").write_text(".orcexe/\n", encoding="utf-8")
        self.git_run(self.main, "add", ".")
        self.git_run(self.main, "commit", "--quiet", "-m", "seed")

    def linked(self, name="linked"):
        directory = self.directory / name
        self.git_run(self.main, "worktree", "add", "--quiet", "--detach", str(directory), "HEAD")
        return directory


class GitUtilTests(GitFixture):
    def test_ls_files_nul_safe_root_relative_and_tracked_only(self):
        names = ["space name.txt", "line\nbreak.txt", "雪-😀.txt", "subdir/inside.py"]
        (self.main / "subdir").mkdir()
        for name in names:
            (self.main / name).write_text("fixture", encoding="utf-8")
        self.git_run(self.main, "add", ".")
        (self.main / "untracked.txt").write_text("untracked", encoding="utf-8")
        self.assertEqual(gitutil.ls_files(self.main), sorted(names))
        self.assertEqual(gitutil.ls_files(self.main / "subdir"), sorted(names))
        with self.assertRaises(gitutil.GitError):
            gitutil.ls_files(self.home)

    def test_ls_files_empty_and_failure(self):
        self.assertEqual(gitutil.ls_files(self.main), [])
        result = proc.ProcResult("timeout", None, 0, 0, 0, 0, "", False, None)
        with mock.patch("orcexe.gitutil.proc.run", return_value=result):
            with self.assertRaises(gitutil.GitError):
                gitutil.ls_files(self.main)

    def test_unborn_branch_and_non_repository_errors(self):
        self.assertTrue(gitutil.is_repo(self.main))
        self.assertIsNone(gitutil.head(self.main))
        self.assertEqual(gitutil.dirty(self.main), [])
        (self.main / "new.txt").write_text("new", encoding="utf-8")
        self.git_run(self.main, "add", "new.txt")
        self.assertEqual(gitutil.changed_since(self.main, None, []),
                         {"head": None, "diff_files": ["new.txt"], "untracked": []})
        self.assertFalse(gitutil.is_repo(self.home))
        for operation in (gitutil.head, gitutil.toplevel, gitutil.main_worktree, gitutil.dirty):
            with self.subTest(operation=operation.__name__), self.assertRaises(gitutil.GitError):
                operation(self.home)
        with self.assertRaises(gitutil.GitError):
            gitutil.changed_since(self.home, None, [])

    def test_porcelain_renames_spaces_newlines_unicode_and_dirty_exclusion(self):
        self.seed()
        names = ["space name.txt", "line\nbreak.txt", "雪-😀.txt"]
        for name in names:
            (self.main / name).write_text("new", encoding="utf-8")
        before = gitutil.head(self.main)
        self.assertEqual(gitutil.dirty(self.main), sorted(names))
        old = "tracked.txt"
        new = "renamed \n雪.txt"
        self.git_run(self.main, "mv", old, new)
        changed = gitutil.changed_since(self.main, before, names[:1])
        self.assertEqual(changed["diff_files"], sorted(names[1:] + [old, new]))
        self.assertEqual(changed["untracked"], sorted(names))
        self.assertEqual(gitutil.dirty(self.main), sorted(names + [old, new]))

    def test_linked_vs_main_and_committed_changes(self):
        self.seed()
        linked = self.linked()
        (linked / "subdir").mkdir()
        self.assertEqual(gitutil.toplevel(linked / "subdir"), str(linked))
        self.assertEqual(gitutil.main_worktree(linked), str(self.main))
        self.assertEqual(gitutil.main_worktree(self.main), str(self.main))
        before = gitutil.head(linked)
        (linked / "tracked.txt").write_text("after", encoding="utf-8")
        self.git_run(linked, "commit", "--quiet", "-am", "change")
        change = gitutil.changed_since(linked, before, [])
        self.assertNotEqual(change["head"], before)
        self.assertEqual(change["diff_files"], ["tracked.txt"])
        self.assertEqual(gitutil.dirty(linked), [])

    def test_incomplete_observations_are_errors_and_env_is_minimal(self):
        result = proc.ProcResult("timeout", None, 0, 0, 0, 0, "", False, None)
        with mock.patch.dict(os.environ, {"AWS_SECRET_ACCESS_KEY": "secret", "GIT_DIR": "bad"}):
            with mock.patch("orcexe.gitutil.proc.run", return_value=result) as run:
                with self.assertRaises(gitutil.GitError):
                    gitutil.dirty(self.main)
                env = run.call_args.kwargs["env"]
                self.assertNotIn("AWS_SECRET_ACCESS_KEY", env)
                self.assertNotIn("GIT_DIR", env)
                self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
        for text in ("R  dest\0", "broken\0"):
            with self.assertRaises(gitutil.GitError):
                gitutil._porcelain(text)
