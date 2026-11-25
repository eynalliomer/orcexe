"""R9: bind ignore authority to base blobs at the actual Git spawn boundary."""

import os
from pathlib import Path
import subprocess
from unittest import mock

from tests.unit.test_gate_ignore import IgnoreFixture, ROOT_RULES, NESTED_RULES


class IgnoreAuthorityTests(IgnoreFixture):
    def test_snapshot_never_uses_executor_ignore_sources(self):
        self.write(self.path, '.gitignore', 'root-hidden.txt\n')
        self.write(self.path, 'nested/.gitignore', '*\n')
        self.write(self.path, 'new/.gitignore', '*\n')
        self.write(self.main / '.git', 'info/exclude', 'info-hidden.txt\n')
        registration = Path(self.git_run(self.path, 'rev-parse', '--absolute-git-dir').strip())
        self.write(registration, 'info/exclude', 'local-info-hidden.txt\n')
        excludes = self.directory / 'hostile-excludes'
        excludes.write_text('config-hidden.txt\n')
        self.git_run(self.main, 'config', 'core.excludesFile', str(excludes))
        global_config = self.directory / 'hostile-global'
        global_config.write_text('[core]\n\texcludesFile = ' + str(excludes) + '\n')
        visible = {'root-hidden.txt', 'nested/payload.txt', 'new/payload.txt',
                   'info-hidden.txt', 'local-info-hidden.txt', 'config-hidden.txt'}
        ignored = {'root.cache', 'nested/base.log', 'build/deep/output.txt'}
        for name in visible | ignored:
            self.write(self.path, name, 'candidate\n')
        real_spawn = subprocess.Popen
        observations = []

        def spawn(argv, *args, **kwargs):
            if 'check-ignore' in argv:
                root = Path(kwargs['cwd'])
                self.assertNotEqual(root, self.path)
                self.assertNotEqual(root, self.main)
                self.assertEqual((root / '.gitignore').read_text(), ROOT_RULES)
                self.assertEqual((root / 'nested/.gitignore').read_text(), NESTED_RULES)
                self.assertFalse((root / 'new/.gitignore').exists())
                self.assertFalse((root / '.git/info/exclude').exists())
                self.assertEqual(sorted(p.relative_to(root).as_posix() for p in root.rglob('.gitignore')),
                                 ['.gitignore', 'nested/.gitignore'])
                self.assertIn('core.excludesFile=' + os.devnull, argv)
                self.assertIn('--no-index', argv)
                env = kwargs['env']
                self.assertEqual(env['GIT_CONFIG_GLOBAL'], os.devnull)
                self.assertEqual(env['GIT_CONFIG_SYSTEM'], os.devnull)
                self.assertNotIn('GIT_CONFIG_COUNT', env)
                self.assertNotIn('GIT_DIR', env)
                self.assertNotIn('GIT_WORK_TREE', env)
                observations.append(root)
            self.assertNotIn('--exclude-standard', argv)
            return real_spawn(argv, *args, **kwargs)

        with mock.patch.dict(os.environ, GIT_CONFIG_GLOBAL=str(global_config),
                             GIT_CONFIG_COUNT='1', GIT_CONFIG_KEY_0='core.excludesFile',
                             GIT_CONFIG_VALUE_0=str(excludes), GIT_DIR=str(registration),
                             GIT_WORK_TREE=str(self.path)), \
                mock.patch.object(subprocess, 'Popen', side_effect=spawn):
            observed = self.observe()
        self.assertEqual(len(observations), 1, 'reach the real ignore evaluator')
        self.assertEqual(set(observed['outside']),
                         visible | {'.gitignore', 'nested/.gitignore', 'new/.gitignore'})
        self.assertTrue(self.tree_names(observed['tree']).isdisjoint(ignored))
