"""Only base-commit ignore rules can remove untracked snapshot candidates."""

from dataclasses import replace
import os
from pathlib import Path
import tempfile
import time
from unittest import mock

from tests.unit.test_gitutil import GitFixture
from orcexe import gate_lite, task_gate, worktree


ROOT_RULES = 'tests/.durations.json\n/build/\n*.cache\ntracked.txt\n'
NESTED_RULES = '*.log\n!keep.log\n/deep/\n'


class IgnoreFixture(GitFixture):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.write(self.main, '.gitignore', ROOT_RULES)
        self.write(self.main, 'nested/.gitignore', NESTED_RULES)
        self.write(self.main, 'tracked.txt', 'before\n')
        self.write(self.main, 'value.txt', 'before\n')
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'add', '-f', 'tracked.txt')
        self.git_run(self.main, 'commit', '-qm', 'base ignore rules')
        self.wt = worktree.create(self.main, worktree.Spec('plan', 'task', 'fake'))
        self.path = Path(self.wt.path)

    @staticmethod
    def write(root, name, contents):
        file = root / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(contents)

    def observe(self):
        return task_gate.observe(self.main, self.wt, ['value.txt'])

    def tree_names(self, tree):
        return set(self.git_run(self.path, 'ls-tree', '-rz', '--name-only', tree).split('\0')[:-1])


class BaseIgnoreTests(IgnoreFixture):
    def test_base_root_nested_directory_and_negation_rules(self):
        ignored = {'tests/.durations.json', 'build/nested/output.txt', 'root.cache',
                   'nested/output.log', 'nested/child/output.log', 'nested/deep/file.txt'}
        visible = {'nested/keep.log', 'elsewhere/output.log', 'other/build/output.txt'}
        for name in ignored | visible:
            self.write(self.path, name, 'generated\n')
        self.write(self.path, 'value.txt', 'after\n')
        observed = self.observe()
        self.assertEqual(set(observed['outside']), visible)
        self.assertEqual({f['path'] for f in observed['files']}, visible | {'value.txt'})
        self.assertTrue(self.tree_names(observed['tree']).isdisjoint(ignored))
        # Gate copies consume the same candidate tree and cannot receive caches.
        with tempfile.TemporaryDirectory() as temporary:
            checkout = gate_lite._checkout(self.main, observed['tree'], Path(temporary), {},
                                           time.monotonic() + 30)
            self.assertTrue(all(not (checkout / name).exists() for name in ignored))

    def test_edited_root_rules_neither_add_nor_remove_exemptions(self):
        self.write(self.path, '.gitignore', 'hidden.txt\n')
        self.write(self.path, 'hidden.txt', 'must count\n')
        self.write(self.path, 'root.cache', 'still ignored by base\n')
        observed = self.observe()
        self.assertEqual(set(observed['outside']), {'.gitignore', 'hidden.txt'})
        self.assertNotIn('root.cache', self.tree_names(observed['tree']))

    def test_new_and_edited_nested_rules_cannot_hide_files(self):
        self.write(self.path, 'new/.gitignore', '*\n')
        self.write(self.path, 'new/payload.txt', 'must count\n')
        self.write(self.path, 'nested/.gitignore', '*\n')
        self.write(self.path, 'nested/payload.txt', 'must count\n')
        self.write(self.path, 'nested/output.log', 'base ignored\n')
        self.assertEqual(set(self.observe()['outside']),
                         {'new/.gitignore', 'new/payload.txt', 'nested/.gitignore', 'nested/payload.txt'})

    def test_info_config_global_and_environment_excludes_do_not_apply(self):
        self.write(self.main / '.git', 'info/exclude', 'info-hidden.txt\n')
        excludes = self.directory / 'excludes'
        excludes.write_text('config-hidden.txt\n')
        self.git_run(self.main, 'config', 'core.excludesFile', str(excludes))
        global_config = self.directory / 'global-config'
        global_config.write_text('[core]\n\texcludesFile = ' + str(excludes) + '\n')
        names = {'info-hidden.txt', 'config-hidden.txt'}
        for name in names:
            self.write(self.path, name, 'must count\n')
        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config),
                             GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.excludesFile',
                             GIT_CONFIG_VALUE_0=str(excludes)):
            self.assertEqual(set(self.observe()['outside']), names)

    def test_tracked_ignored_files_remain_observed_even_if_removed_from_index(self):
        self.write(self.path, 'tracked.txt', 'modified\n')
        for removed in (False, True):
            with self.subTest(removed=removed):
                if removed:
                    self.git_run(self.path, 'rm', '--cached', 'tracked.txt')
                observed = self.observe()
                self.assertIn('tracked.txt', observed['outside'])
                self.assertIn('tracked.txt', self.tree_names(observed['tree']))
                self.assertIn('M', [f['status'] for f in observed['files'] if f['path'] == 'tracked.txt'])

    def test_newly_staged_ignored_file_is_observed(self):
        self.write(self.path, 'staged.cache', 'tracked in index\n')
        self.git_run(self.path, 'add', '-f', 'staged.cache')
        self.assertEqual(self.observe()['outside'], ['staged.cache'])

    def test_ignore_input_preserves_newlines_unicode_and_pathspec_characters(self):
        ignored = {'line\nbreak.cache', 'ö.cache', ':(glob)*.cache'}
        visible = {'line\nbreak.txt', 'ö.txt', ':(glob)*.txt'}
        for name in ignored | visible:
            self.write(self.path, name, 'candidate\n')
        observed = self.observe()
        self.assertEqual(set(observed['outside']), visible)
        self.assertTrue(self.tree_names(observed['tree']).isdisjoint(ignored))

    def test_symlinked_base_ignore_is_not_followed(self):
        self.write(self.path, 'external-rules', 'hidden.txt\n')
        (self.path / 'linkdir').mkdir()
        (self.path / 'linkdir/.gitignore').symlink_to('../external-rules')
        self.git_run(self.path, 'add', 'external-rules', 'linkdir/.gitignore')
        self.git_run(self.path, 'commit', '-qm', 'symlinked ignore')
        self.wt = replace(self.wt, base_commit=self.git_run(self.path, 'rev-parse', 'HEAD').strip())
        self.write(self.path, 'linkdir/hidden.txt', 'must count\n')
        self.assertEqual(self.observe()['outside'], ['linkdir/hidden.txt'])

    def test_base_is_used_after_executor_commits_changed_rules(self):
        self.write(self.path, '.gitignore', ROOT_RULES + 'hidden.txt\n')
        self.git_run(self.path, 'commit', '-qam', 'executor rules')
        self.write(self.path, 'hidden.txt', 'must count\n')
        self.assertEqual(set(self.observe()['outside']), {'.gitignore', 'hidden.txt'})

    def test_snapshot_keeps_deadline_discipline(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(gate_lite._ObservationError, 'total_timeout'):
                gate_lite._snapshot(self.path, Path(temporary), time.monotonic() - 1,
                                    self.wt.base_commit)
