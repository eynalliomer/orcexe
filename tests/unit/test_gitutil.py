"""Real temporary repositories, including paths unsafe for line-based parsing."""

import os
import hashlib
import json
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit._timing import scaled
from tests.unit._fixtures import GitTemplates, git_environment, non_repository_root

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe import gitutil, proc


class GitFixture(unittest.TestCase):
    """Reusable hermetic Git setup; never writes the source repository's .git."""

    @classmethod
    def setUpClass(cls):
        cls.templates = GitTemplates(cls.addClassCleanup)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orcexe-t11-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        non_repository_root(self.directory)
        self.main = self.directory / "main"
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
        self.git = git_environment(self.env, self.directory)
        if not self.git:
            self.skipTest("git unavailable")
        def build(directory):
            self.git_run(directory, "init", "--quiet", "--template=")
            self.git_run(directory, "config", "user.name", "Fixture")
            self.git_run(directory, "config", "user.email", "fixture@example.invalid")
            self.git_run(directory, "config", "commit.gpgsign", "false")
        self.templates.copy('empty', self.main, build)
        self.git_run(self.main, "config", "core.hooksPath", str(self.directory / "no-hooks"))

    def git_run(self, directory, *args):
        return subprocess.run([self.git, *args], cwd=str(directory), env=self.env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            check=True, timeout=scaled(5)).stdout.decode("utf-8")

    def copy_fixture(self, source):
        """Copy a prepared main + linked tree, rebinding Git's absolute paths."""
        temporary = tempfile.TemporaryDirectory(prefix='orcexe-git-copy-')
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        old, new = str(source.directory), str(self.directory)
        shutil.copytree(source.directory, self.directory, symlinks=True, dirs_exist_ok=True)
        self.main = self.directory / source.main.relative_to(source.directory)
        self.home = self.directory / source.home.relative_to(source.directory)
        self.env = {key: value.replace(old, new) for key, value in source.env.items()}
        self.git = source.git
        for entry in self.directory.rglob('.git'):
            if entry.is_file():
                entry.write_text(entry.read_text().replace(old, new))
        common = self.main / '.git'
        config = common / 'config'
        config.write_text(config.read_text().replace(old, new))
        for entry in (common / 'worktrees').glob('*/gitdir'):
            entry.write_text(entry.read_text().replace(old, new))
        receipts = common / 'info/orcexe-worktrees'
        for entry in receipts.glob('*.json'):
            data = json.loads(entry.read_text())
            data['path'] = data['path'].replace(old, new)
            mode = entry.stat().st_mode & 0o777
            entry.unlink()
            key = hashlib.sha256(os.fsencode(data['path'])).hexdigest()
            rebound = receipts / (key + '.json')
            rebound.write_text(json.dumps(data) + '\n')
            rebound.chmod(mode)

    def seed(self):
        shutil.rmtree(self.main)
        # Build from the pristine empty template, never from a previous test.
        def seeded(directory):
            shutil.copytree(self.templates.paths['empty'], directory, dirs_exist_ok=True)
            (directory / "tracked.txt").write_text("before\n", encoding="utf-8")
            (directory / ".gitignore").write_text(".orcexe/\n", encoding="utf-8")
            self.git_run(directory, "add", ".")
            self.git_run(directory, "commit", "--quiet", "-m", "seed")
        self.templates.copy('seeded', self.main, seeded)
        self.git_run(self.main, "config", "core.hooksPath", str(self.directory / "no-hooks"))

    def linked(self, name="linked"):
        directory = self.directory / name
        self.git_run(self.main, "worktree", "add", "--quiet", "--detach", str(directory), "HEAD")
        return directory


class GitUtilTests(GitFixture):
    def test_unreadable_fingerprint_is_stable_and_compared_by_marker(self):
        self.seed()
        target = self.main / "unreadable.txt"
        target.write_text("already dirty\n")
        before = gitutil.head(self.main)
        dirty = gitutil.dirty(self.main)
        original_open = Path.open

        def unreadable(path, *args, **kwargs):
            if path == target:
                raise PermissionError("unreadable fixture")
            return original_open(path, *args, **kwargs)

        with mock.patch.object(Path, "open", unreadable):
            fingerprints = gitutil.fingerprints(self.main, dirty)
            self.assertEqual(fingerprints, {"unreadable.txt": "unreadable"})
            self.assertEqual(gitutil.changed_since(self.main, before, dirty, fingerprints)["diff_files"], [])
        self.assertEqual(gitutil.changed_since(self.main, before, dirty, fingerprints)["diff_files"],
                         ["unreadable.txt"])

    def test_non_regular_fingerprints_never_open_fifo_or_socket(self):
        for kind in (stat.S_IFIFO, stat.S_IFSOCK):
            with self.subTest(kind=kind), \
                    mock.patch.object(Path, "lstat", return_value=os.stat_result((kind,) + (0,) * 9)), \
                    mock.patch.object(Path, "open", side_effect=AssertionError("non-regular file opened")):
                self.assertEqual(gitutil.fingerprints(self.main, ["special"]), {"special": "non-regular"})

    def test_fingerprint_lstat_oserror_is_unreadable(self):
        with mock.patch.object(Path, "lstat", side_effect=OSError("fixture stat failed")), \
                mock.patch.object(Path, "open", side_effect=AssertionError("unstatable file opened")):
            self.assertEqual(gitutil.fingerprints(self.main, ["special"]), {"special": "unreadable"})

    def test_dirty_content_edits_deletions_and_untouched_paths(self):
        self.seed()
        (self.main / "tracked.txt").write_bytes(b"already dirty\n")
        (self.main / "untouched.txt").write_bytes(b"untouched\n")
        (self.main / "deleted.txt").write_bytes(b"to delete\n")
        before = gitutil.head(self.main)
        dirty = gitutil.dirty(self.main)
        fingerprints = gitutil.fingerprints(self.main, dirty)
        self.assertEqual(gitutil.changed_since(self.main, before, dirty, fingerprints)["diff_files"], [])
        (self.main / "tracked.txt").write_bytes(b"new content\n")
        (self.main / "deleted.txt").unlink()
        changed = gitutil.changed_since(self.main, before, dirty, fingerprints)
        self.assertEqual(changed["diff_files"], ["deleted.txt", "tracked.txt"])
        # Reverting a dirty tracked file to HEAD still counts as a run edit.
        (self.main / "tracked.txt").write_bytes(b"before\n")
        self.assertEqual(gitutil.changed_since(self.main, before, dirty, fingerprints)["diff_files"],
                         ["deleted.txt", "tracked.txt"])

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


class FakeGitStderrTests(unittest.TestCase):
    """is_repo classification and GitError diagnostics from fake git stderr (T63b)."""

    def classify(self, returncode, stderr, path="/work/repo"):
        result = proc.ProcResult("exited", returncode, 0, 0, 0, 0, stderr, False, None)
        with mock.patch("orcexe.gitutil.proc.run", return_value=result) as run:
            try:
                return gitutil.is_repo(path)
            finally:
                env = run.call_args.kwargs["env"]
                self.assertEqual((env["LC_ALL"], env["LANG"]), ("C", "C"))
                self.assertNotIn("LANGUAGE", env)

    def test_not_a_repository_variants_are_false(self):
        variants = {
            "plain": "fatal: not a git repository (or any of the parent directories): .git\n",
            "invalid gitfile": "fatal: not a git repository: (null)\n",
            "gitfile path": "fatal: not a git repository: '/work/x/not-a-repository'\n",
            "upstream invalid gitfile": ("fatal: gitfile does not point to a valid repository: "
                                         "/tmp/tmpab12cd/.git\n"),
            "upstream invalid gitfile format": "fatal: invalid gitfile format: /tmp/tmpab12cd/.git\n",
            "not a valid repository": "fatal: '/work/x/not-a-repository' not a valid repository\n",
            "case and wrapping": "FATAL: Not a Git\n  repository (or any parent up to mount point /)\n",
            "filesystem boundary": ("fatal: not a git repository (or any parent up to mount point /mnt)\n"
                                    "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n"),
            "boundary only": "Stopping at filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM not set).\n",
            "ceiling": "fatal: not a git repository (or any of the parent directories): .git\n",
            "home config warnings first": (
                "warning: unable to access '/home/u/.config/git/attributes': Permission denied\n"
                "warning: unable to access '/home/u/.config/git/ignore': Permission denied\n"
                "fatal: not a git repository (or any of the parent directories): .git\n"),
        }
        for name, stderr in variants.items():
            with self.subTest(name):
                self.assertFalse(self.classify(128, stderr))

    def test_cut_tail_accepts_discovery_suffix_only_when_cut(self):
        cut = "x" * (gitutil._STDERR_BYTES - 40) + "t repository (or any of the parent directories): .git\n"
        self.assertGreaterEqual(len(cut.encode()), gitutil._STDERR_BYTES)
        self.assertFalse(self.classify(128, cut))
        with self.assertRaises(gitutil.GitError):
            self.classify(128, "fatal: t repository (or any of the parent directories): .git\n")

    def test_ownership_and_other_failures_are_errors_with_diagnostics(self):
        dubious = ("fatal: detected dubious ownership in repository at '/work/repo'\n"
                   "To add an exception for this directory, call:\n\n"
                   "\tgit config --global --add safe.directory /work/repo\n")
        with self.assertRaises(gitutil.GitError) as caught:
            self.classify(128, dubious)
        self.assertEqual(caught.exception.reason, "Repository ownership is not trusted (safe.directory)")
        self.assertEqual(caught.exception.returncode, 128)
        self.assertIn("dubious ownership in repository at '<path>'", str(caught.exception))
        self.assertNotIn("/work/repo", str(caught.exception))
        # Ownership wins even when the same stderr also names an invalid gitfile.
        with self.assertRaises(gitutil.GitError) as caught:
            self.classify(128, dubious + "fatal: gitfile does not point to a valid repository: x\n")
        self.assertEqual(caught.exception.reason, "Repository ownership is not trusted (safe.directory)")
        for returncode, stderr in ((128, "fatal: unable to read current working directory\n"),
                                   (128, ""), (1, "fatal: not a git repository\n"),
                                   (1, "fatal: gitfile does not point to a valid repository: x\n"),
                                   (129, "usage: git rev-parse\n")):
            with self.subTest(returncode=returncode, stderr=stderr), \
                    self.assertRaises(gitutil.GitError) as caught:
                self.classify(returncode, stderr)
            self.assertEqual(caught.exception.reason, "Cannot determine whether path is a repository")
            self.assertIn("(exit {})".format(returncode), str(caught.exception))

    def test_diagnostic_is_short_single_line_redacted_and_path_free(self):
        home = str(Path.home())
        message = ("fatal: cannot open '{}/secret/project/.git/HEAD': token=ghp_{}\n"
                   "fatal: in /work/repo/sub\n").format(home, "A1b2C3d4" * 5)
        stderr = message + "-" * 1000
        with mock.patch.dict(os.environ, {"HOME": home}):
            text = gitutil._diagnostic(stderr, "/work/repo")
        self.assertLessEqual(len(text), gitutil._DIAGNOSTIC_CHARS)
        self.assertNotIn("\n", text)
        with mock.patch.dict(os.environ, {"HOME": home}):
            short = gitutil._diagnostic(message, "/work/repo")
        self.assertNotIn(home, short)
        self.assertIn("~/secret/project", short)
        self.assertIn("<path>/sub", short)
        self.assertNotIn("A1b2C3d4" * 5, short)
        result = proc.ProcResult("exited", 128, 0, 0, 0, 0, "fatal: bad object HEAD\n", False, None)
        with mock.patch("orcexe.gitutil.proc.run", return_value=result), \
                self.assertRaises(gitutil.GitError) as caught:
            gitutil.toplevel("/work/repo")
        self.assertEqual(str(caught.exception), "Git rev-parse failed (exit 128): fatal: bad object HEAD")
        self.assertEqual(caught.exception.stderr_tail, "fatal: bad object HEAD")
