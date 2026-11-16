"""Hermetic lifecycle and restore proofs using real, temporary Git repositories."""

from dataclasses import FrozenInstanceError, replace
import hashlib
import json
import os
from pathlib import Path
import stat
import unittest
from unittest import mock

from tests.unit.test_gitutil import GitFixture
from orcexe import proc, worktree


class IdentityTests(unittest.TestCase):
    def test_spec_validation_and_frozen_records(self):
        for name in ("plan", "task", "tool", "cand"):
            for value in ("../", "a/b", "Upper", "", "a" * 41, "a..b", "x\n", 1):
                with self.subTest(name=name, value=value), self.assertRaises(ValueError):
                    worktree.Spec(**dict(dict(plan="p", task="t", tool="x"), **{name: value}))
        for name in ("plan", "task", "tool"):
            with self.assertRaises(ValueError):
                worktree.Spec(**dict(dict(plan="p", task="t", tool="x"), **{name: None}))
        spec = worktree.Spec("p", "t", "x", "a-._9")
        with self.assertRaises(FrozenInstanceError):
            spec.task = "other"
        wt = worktree.Worktree("path", "branch", "0" * 40, spec)
        with self.assertRaises(FrozenInstanceError):
            wt.path = "other"

    def test_runner_failure_and_explicit_environment(self):
        result = proc.ProcResult("timeout", None, 0, 0, 0, 0, "", False, None)
        with mock.patch.dict(os.environ, {"GIT_DIR": "wrong", "TOKEN": "secret"}):
            with mock.patch("orcexe.worktree.proc.run", return_value=result) as runner:
                with self.assertRaises(worktree.WorktreeError):
                    worktree.status(worktree.Worktree("unused", "unused", "0" * 40,
                                                     worktree.Spec("p", "t", "x")))
                env = runner.call_args.kwargs["env"]
                self.assertNotIn("GIT_DIR", env)
                self.assertNotIn("TOKEN", env)
                self.assertEqual(env["GIT_CONFIG_GLOBAL"], os.devnull)
                self.assertIsInstance(runner.call_args.args[0], list)


class WorktreeTests(GitFixture):
    def setUp(self):
        super().setUp()
        renamed = self.directory / "main space 雪\nline"
        self.main.rename(renamed)
        self.main = renamed
        (self.main / "tracked.txt").write_bytes(b"before\n")
        (self.main / "deleted.txt").write_bytes(b"delete me\n")
        (self.main / ".gitignore").write_bytes(b"ignored*\n")
        self.git_run(self.main, "add", ".")
        self.git_run(self.main, "commit", "--quiet", "-m", "seed")
        self.base = self.git_run(self.main, "rev-parse", "HEAD").strip()
        self.archive_root = self.main / ".orcexe" / "archive"

    def make(self, task="task", cand=None, repo=None):
        return worktree.create(repo or self.main, worktree.Spec("plan", task, "fake", cand))

    def branch_exists(self, branch):
        return "refs/heads/" + branch in self.git_run(self.main, "show-ref")

    def snapshot(self, directory):
        # Every nonignored file is compared, including binary bytes and names.
        names = self.git_run(directory, "ls-files", "-z", "--cached", "--others",
                             "--exclude-standard").split("\0")[:-1]
        return {name: hashlib.sha256((Path(directory) / name).read_bytes()).hexdigest()
                for name in set(names) if (Path(directory) / name).is_file()}

    def test_create_list_status_layout_linked_origin_and_exclude(self):
        before = self.git_run(self.main, "status", "--porcelain", "-z")
        exclude = self.main / ".git" / "info" / "exclude"
        exclude.parent.mkdir(exist_ok=True)
        exclude.write_bytes(b"# preserve existing line endings\r\n")
        first = self.make(cand="c1")
        self.assertTrue(exclude.read_bytes().startswith(b"# preserve existing line endings\r\n"))
        # An existing CRLF rule also counts as present on subsequent creates.
        exclude.write_bytes(exclude.read_bytes().replace(b"/.orcexe/\n", b"/.orcexe/\r\n"))
        root = self.main.parent / (self.main.name + "-wt")
        self.assertEqual(first.path, str(root / "plan" / "task-c1-fake"))
        self.assertEqual(first.branch, "orcexe/plan/task-c1-fake")
        self.assertEqual(first.base_commit, self.base)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o755)
        second = self.make("two", repo=first.path)
        third = self.make("three")
        self.assertEqual(second.path, str(root / "plan" / "two-fake"))
        self.assertEqual(set(worktree.list_managed(first.path)), {first, second, third})
        self.assertTrue(worktree.is_managed(self.main, first.path))
        self.assertEqual(exclude.read_text().splitlines().count("/.orcexe/"), 1)
        self.assertEqual(before, self.git_run(self.main, "status", "--porcelain", "-z"))
        (self.main / ".orcexe").mkdir()
        (self.main / ".orcexe" / "state").write_bytes(b"ignored metadata")
        self.assertEqual(before, self.git_run(self.main, "status", "--porcelain", "-z"))
        (Path(first.path) / "space 雪\nnew.txt").write_bytes(b"new\n")
        (Path(first.path) / "tracked.txt").write_bytes(b"changed\n")
        index = Path(self.git_run(first.path, "rev-parse", "--git-path", "index").rstrip("\n"))
        index_before = index.read_bytes()
        self.assertEqual(worktree.status(first), {
            "head": self.base, "base_commit": self.base, "commits_ahead": 0,
            "dirty": ["space 雪\nnew.txt", "tracked.txt"]})
        self.assertEqual(index.read_bytes(), index_before)

    def test_existing_branch_path_unborn_and_append_only_receipt(self):
        wt = self.make()
        with self.assertRaisesRegex(worktree.WorktreeError, "branch_exists.*" + wt.branch):
            self.make()
        target = Path(wt.path).parent / "occupied-fake"
        target.mkdir()
        with self.assertRaisesRegex(worktree.WorktreeError, "path_exists"):
            self.make("occupied")
        target.rmdir()
        target.symlink_to(self.home, target_is_directory=True)
        with self.assertRaisesRegex(worktree.WorktreeError, "path_exists"):
            self.make("occupied")
        unborn = self.directory / "unborn"
        unborn.mkdir()
        self.git_run(unborn, "init", "--quiet", "--template=")
        with self.assertRaisesRegex(worktree.WorktreeError, "base_commit_unresolved.*unborn"):
            worktree.create(unborn, wt.spec)
        worktree.remove(self.main, wt, archive_root=self.archive_root)
        with self.assertRaisesRegex(worktree.WorktreeError, "attempt_exists"):
            self.make()

    def test_guards_and_unowned_worktrees_survive_remove(self):
        wt = self.make()
        with self.assertRaisesRegex(worktree.WorktreeError, "^main_worktree_write_refused$"):
            worktree.assert_writable(self.main / "not-created" / "file")
        self.assertEqual(worktree.assert_writable(Path(wt.path) / "new"),
                         {"worktree": wt.path, "relative_path": "new"})
        link = Path(wt.path) / "main-link"
        link.symlink_to(self.main, target_is_directory=True)
        with self.assertRaisesRegex(worktree.WorktreeError, "^main_worktree_write_refused$"):
            worktree.assert_writable(link / "tracked.txt")
        feature = self.directory / "feature"
        outside = self.directory / "outside"
        impostor = Path(wt.path).parent / "impostor-fake"
        for path, branch in ((feature, "feature/x"), (outside, "orcexe/outside"),
                             (impostor, "orcexe/plan/impostor-fake")):
            self.git_run(self.main, "worktree", "add", "--quiet", "-b", branch, str(path))
        for path, branch in ((self.main, "unused"), (feature, "feature/x"),
                             (outside, "orcexe/outside"), (impostor, "orcexe/plan/impostor-fake")):
            with self.subTest(path=path):
                self.assertFalse(worktree.is_managed(self.main, path))
                foreign = replace(wt, path=str(path), branch=branch)
                with self.assertRaisesRegex(worktree.WorktreeError, "unmanaged_worktree"):
                    worktree.remove(self.main, foreign, archive_root=self.archive_root)
                self.assertTrue(path.is_dir())
        with self.assertRaisesRegex(worktree.WorktreeError, "unmanaged_worktree"):
            worktree.remove(self.main, replace(wt, branch="feature/x"), archive_root=self.archive_root)
        self.assertTrue(self.branch_exists("feature/x"))
        self.assertEqual(worktree.list_managed(self.main), [wt])
        self.assertEqual(worktree.assert_writable(feature)["worktree"], str(feature))

    def test_missing_registered_entry_reported(self):
        wt = self.make()
        moved = Path(wt.path).with_name("moved-out-of-the-way")
        Path(wt.path).rename(moved)
        self.assertEqual(worktree.list_managed(self.main), [replace(wt, missing=True)])
        self.assertTrue(worktree.is_managed(self.main, wt.path))
        with self.assertRaisesRegex(worktree.WorktreeError, "archive_unverified:"):
            worktree.remove(self.main, wt, archive_root=self.archive_root)
        self.assertTrue(self.branch_exists(wt.branch))
        self.assertTrue(moved.is_dir())

    def committed_changes(self, wt, count=2):
        path = Path(wt.path)
        for number in range(1, count + 1):
            (path / "tracked.txt").write_bytes(("commit {}\n".format(number)).encode())
            self.git_run(path, "commit", "--quiet", "-am", "commit {}".format(number))

    def test_archive_restore_property(self):
        wt = self.make(cand="candidate")
        self.committed_changes(wt)
        path = Path(wt.path)
        (path / "tracked.txt").write_bytes(b"uncommitted\r\nlast line\x00\xff")
        (path / "new.txt").write_bytes(b"untracked\n")
        (path / "binary.bin").write_bytes(bytes(range(256)) * 8)
        (path / "deleted.txt").unlink()
        (path / "space 雪\nfile.txt").write_bytes(b"special path\n")
        (path / "ignored-secret").write_bytes(b"not archived")
        # Preserve executable mode and symlink state as well as file bytes.
        (path / "new.txt").chmod(0o755)
        (path / "link").symlink_to("new.txt")
        expected = self.snapshot(path)
        state = worktree.status(wt)
        self.assertEqual(state["commits_ahead"], 2)
        result = worktree.remove(self.main, wt, archive_root=self.archive_root)
        dest = Path(result["archived_to"])
        self.assertTrue(result["removed"])
        self.assertTrue(result["verified"])
        self.assertFalse(path.exists())
        self.assertFalse(self.branch_exists(wt.branch))
        self.assertEqual(worktree.list_managed(self.main), [])
        self.assertTrue(dest.name.startswith("task-candidate-fake-"))
        self.assertTrue(dest.name.endswith("Z"))
        self.assertEqual(dest.parent.name, "plan")
        self.assertEqual(stat.S_IMODE(dest.stat().st_mode), 0o700)
        manifest = json.loads((dest / "manifest.json").read_text())
        self.assertEqual(manifest["counts"]["commits_ahead"], 2)
        self.assertEqual(manifest["counts"]["ignored"], 1)
        self.assertEqual(manifest["head"], state["head"])
        self.assertEqual(manifest["base_commit"], self.base)
        self.assertEqual(manifest["spec"], dict(plan="plan", task="task", tool="fake", cand="candidate"))
        for name, digest in manifest["sha256"].items():
            self.assertEqual(hashlib.sha256((dest / name).read_bytes()).hexdigest(), digest)
        restore = self.directory / "restore"
        self.git_run(self.main, "worktree", "add", "--quiet", "--detach", str(restore), self.base)
        self.git_run(restore, "fetch", str(dest / "commits.bundle"), "refs/heads/" + wt.branch)
        self.git_run(restore, "merge", "--ff-only", "FETCH_HEAD")
        self.git_run(restore, "apply", "--binary", str(dest / "worktree.patch"))
        self.assertEqual(self.snapshot(restore), expected)
        self.assertEqual((restore / "new.txt").stat().st_mode & 0o111, 0o111)
        self.assertTrue((restore / "link").is_symlink())
        self.assertEqual(self.git_run(self.main, "status", "--porcelain"), "")

    def test_clean_empty_archive_and_destination_no_overwrite(self):
        wt = self.make()
        dest = self.directory / "archive"
        dest.mkdir()
        with self.assertRaisesRegex(worktree.WorktreeError, "archive_exists"):
            worktree.archive(self.main, wt, dest)
        with self.assertRaisesRegex(worktree.WorktreeError, "unsafe_archive_destination"):
            worktree.archive(self.main, wt, Path(wt.path) / "archive")
        alias = self.directory / "archive alias"
        alias.symlink_to(self.archive_root, target_is_directory=True)
        result = worktree.remove(self.main, wt, archive_root=alias)
        dest = Path(result["archived_to"])
        self.assertEqual(dest.parent.parent, self.archive_root)
        self.assertFalse(result["bundle_created"])
        self.assertEqual(result["counts"], dict(commits_ahead=0, dirty=0, ignored=0))
        self.assertEqual((dest / "worktree.patch").read_bytes(), b"")
        self.assertFalse((dest / "commits.bundle").exists())
        self.assertFalse(Path(wt.path).exists())

    def test_verification_failure_never_deletes_worktree_or_branch(self):
        for failure in ("bundle", "patch", "checksum"):
            with self.subTest(failure=failure):
                wt = self.make(failure)
                self.committed_changes(wt, count=1)
                (Path(wt.path) / "tracked.txt").write_bytes(b"dirty\n")
                expected = self.snapshot(wt.path)
                real_run = proc.run

                def sabotage(argv, **kwargs):
                    if failure == "bundle" and argv[2:4] == ["bundle", "verify"]:
                        return proc.ProcResult("exited", 1, 0, 0, 0, 0, "forced bundle failure", False, None)
                    if argv[2] == "read-tree":
                        patch = next(self.archive_root.glob("plan/{}-fake-*/worktree.patch".format(failure)))
                        if failure == "patch":
                            patch.write_bytes(b"corrupt patch\n")
                        if failure == "checksum":
                            patch.write_bytes(patch.read_bytes() + b"\n")
                    return real_run(argv, **kwargs)

                with mock.patch("orcexe.worktree.proc.run", side_effect=sabotage):
                    reason = {"bundle": "forced bundle failure", "patch": "git apply failed",
                              "checksum": "sha256 mismatch"}[failure]
                    with self.assertRaisesRegex(worktree.WorktreeError,
                                                "(?s)^archive_unverified:.*" + reason):
                        worktree.remove(self.main, wt, archive_root=self.archive_root)
                self.assertTrue(Path(wt.path).is_dir())
                self.assertTrue(self.branch_exists(wt.branch))
                self.assertEqual(self.snapshot(wt.path), expected)
                self.assertEqual(len(list(self.archive_root.glob("plan/{}-fake-*/manifest.json".
                                                            format(failure)))), 1)

    def test_post_archive_failures_keep_verified_backup_and_describe_remaining_state(self):
        for step in ("worktree", "branch"):
            with self.subTest(step=step):
                wt = self.make(step)
                real_run = proc.run

                def fail_delete(argv, **kwargs):
                    if ((step == "worktree" and argv[2:4] == ["worktree", "remove"]) or
                            (step == "branch" and argv[2:4] == ["branch", "-D"])):
                        return proc.ProcResult("exited", 1, 0, 0, 0, 0, "forced deletion failure", False, None)
                    return real_run(argv, **kwargs)

                with mock.patch("orcexe.worktree.proc.run", side_effect=fail_delete):
                    with self.assertRaisesRegex(worktree.WorktreeError,
                                                "(?s)archive at .*branch remains"):
                        worktree.remove(self.main, wt, archive_root=self.archive_root)
                self.assertTrue(self.branch_exists(wt.branch))
                self.assertEqual(Path(wt.path).exists(), step == "worktree")
                self.assertEqual(len(list(self.archive_root.glob("plan/{}-fake-*/manifest.json".
                                                            format(step)))), 1)

