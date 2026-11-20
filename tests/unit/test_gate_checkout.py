"""Raw committed gate copies refuse escaping links without project execution."""

import os
import subprocess
import time
from unittest import mock

from tests.unit.test_gitutil import GitFixture
from tests.unit._timing import scaled
from orcexe import gate_lite as gate


class CheckoutTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.git_run(self.main, 'config', 'core.precomposeunicode', 'false')
        self.serial = 0

    def entry(self, name, content, mode='100644'):
        result = subprocess.run([self.git, 'hash-object', '-w', '--stdin'],
                                input=content, cwd=str(self.main), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                check=True, timeout=scaled(5))
        oid = result.stdout.decode().strip()
        self.git_run(self.main, 'update-index', '--add', '--cacheinfo', mode + ',' + oid + ',' + name)

    def checkout(self):
        self.serial += 1
        root = self.directory / ('copy-' + str(self.serial))
        root.mkdir()
        tree = self.git_run(self.main, 'write-tree').strip()
        with mock.patch.dict(os.environ, self.env, clear=True):
            return gate._checkout(self.main, tree, root, {}, time.monotonic() + scaled(30))

    def test_attributes_never_transform_blobs_or_run_smudge_filters(self):
        contents = {'ignored.txt': b'raw ignored\n', 'line.txt': b'raw eol\n',
                    'substituted.txt': b'$Format:%H$\n', 'filtered.txt': b'raw filter\n'}
        for name, content in contents.items():
            self.entry(name, content)
        self.entry('.gitattributes', b'ignored.txt export-ignore\nline.txt text eol=crlf\n'
                   b'substituted.txt export-subst\nfiltered.txt filter=attack\n')
        marker = self.directory / 'SMUDGE-RAN'
        self.git_run(self.main, 'config', 'filter.attack.smudge', 'touch ' + str(marker))
        original = gate._git_bytes
        def raw_only(repo, *args, **kwargs):
            self.assertNotIn('archive', args)
            self.assertNotIn('--filters', args)
            self.assertNotIn('--textconv', args)
            return original(repo, *args, **kwargs)
        with mock.patch.object(gate, '_git_bytes', side_effect=raw_only):
            copy = self.checkout()
        for name, content in contents.items():
            self.assertEqual((copy / name).read_bytes(), content)
        self.assertFalse(marker.exists())

    def test_later_dependency_link_and_link_chains_are_refused(self):
        (self.main / 'node_modules').mkdir()
        (self.main / 'wip').mkdir()
        for target in (b'node_modules/../wip', b'indirect', b'node_modules/package'):
            with self.subTest(target=target):
                if target == b'indirect':
                    self.entry('indirect', b'node_modules/../wip', '120000')
                self.entry('escape', target, '120000')
                with self.assertRaisesRegex(gate._ObservationError, 'unsafe_base_symlink'):
                    self.checkout()
                self.git_run(self.main, 'update-index', '--force-remove', '--', 'indirect')
        self.assertEqual(list((self.main / 'wip').iterdir()), [])

    def test_absolute_parent_chained_and_looping_links_are_refused(self):
        for target in (os.fsencode(self.main), b'../main', b'dir/../../outside', b'escape'):
            with self.subTest(target=target):
                self.entry('escape', target, '120000')
                with self.assertRaisesRegex(gate._ObservationError, 'unsafe_base_symlink'):
                    self.checkout()
        self.entry('escape', b'indirect', '120000')
        for target in (b'../outside', b'escape'):
            with self.subTest(chain=target):
                self.entry('indirect', target, '120000')
                with self.assertRaisesRegex(gate._ObservationError, 'unsafe_base_symlink'):
                    self.checkout()

    def test_contained_relative_links_and_chains_keep_raw_targets(self):
        self.entry('dir/file', b'raw\n')
        for name, target in (('link', b'dir/file'), ('chain', b'link'),
                             ('dir/parent', b'../link'), ('dangling', b'missing')):
            self.entry(name, target, '120000')
        copy = self.checkout()
        self.assertEqual((copy / 'chain').read_bytes(), b'raw\n')
        self.assertEqual((copy / 'dir/parent').read_bytes(), b'raw\n')
        self.assertEqual(os.readlink(copy / 'dangling'), 'missing')

    def test_gitlinks_are_refused(self):
        head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        self.git_run(self.main, 'update-index', '--add', '--cacheinfo', '160000,' + head + ',sub')
        with self.assertRaisesRegex(gate._ObservationError, 'unsupported_base_tree_entry'):
            self.checkout()

    def test_case_and_unicode_collisions_never_silently_change_the_copy(self):
        # Populate the Git index directly so even insensitive filesystems have
        # both committed names. A faithful distinct copy or refusal is required.
        for names in (('CASE.txt', 'case.txt'), ('caf\u00e9.txt', 'cafe\u0301.txt')):
            with self.subTest(names=names):
                for name, content in zip(names, (b'first', b'second')):
                    self.entry(name, content)
                tree_names = self.git_run(self.main, 'ls-files', '-z').split('\0')
                self.assertTrue(all(name in tree_names for name in names))
                try:
                    copy = self.checkout()
                except (gate._ObservationError, OSError, ValueError):
                    pass
                else:
                    for name, content in zip(names, (b'first', b'second')):
                        self.assertEqual((copy / name).read_bytes(), content)
                for name in names:
                    self.git_run(self.main, 'update-index', '--force-remove', '--', name)
