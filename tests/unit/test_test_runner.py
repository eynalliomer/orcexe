"""Exercise runner isolation, discovery, failures, deadlines and scheduling."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tests.tools import run_tests as runner


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)

    def module(self, name, source):
        (self.directory / (name + '.py')).write_text(source, encoding='utf-8')
        return name

    def run_module(self, name, timeout=10):
        return runner.run_module(name, str(self.directory), timeout, str(self.directory))

    def test_workers_isolate_homes_and_preserve_flags(self):
        source = '''import os, tempfile, unittest
class Tests(unittest.TestCase):
    def test_home(self):
        from pathlib import Path
        home = Path(os.environ['HOME'])
        self.assertFalse((home / 'previous-worker').exists())
        (home / 'previous-worker').touch()
        self.assertEqual(Path(os.environ['CODEX_HOME']).parent, home)
        self.assertEqual(Path(os.environ['XDG_CACHE_HOME']).parent, home)
        self.assertEqual(tempfile.gettempdir(), os.environ['TMPDIR'])
        self.assertEqual(os.environ['ORCEXE_TEST_TIME_SCALE'], '2.5')
        self.assertEqual(os.environ['ORCEXE_SLOW_TESTS'], '1')
'''
        name = self.module('isolation', source)
        with mock.patch.dict(os.environ, ORCEXE_TEST_TIME_SCALE='2.5', ORCEXE_SLOW_TESTS='1'):
            for _ in range(2):
                record = self.run_module(name)
                self.assertTrue(record['successful'], record)
                self.assertEqual(record['tests'], 1)

    def test_failure_error_and_skip_are_collected_with_tracebacks(self):
        name = self.module('outcomes', '''import unittest
class Tests(unittest.TestCase):
    def test_fail(self): self.fail('expected failure')
    def test_error(self): raise RuntimeError('expected error')
    def test_skip(self): self.skipTest('expected skip')
''')
        record = self.run_module(name)
        self.assertFalse(record['successful'])
        self.assertIsNone(record['issue'])
        self.assertEqual((record['tests'], record['failures'], record['errors'], record['skips']),
                         (3, 1, 1, 1))
        self.assertEqual(len(record['tracebacks']), 2)
        self.assertIn('expected failure', record['output'])
        self.assertIn('expected error', record['output'])

    def test_crash_and_timeout_fail_without_results(self):
        crash = self.module('crash', 'import os; os._exit(7)\n')
        record = self.run_module(crash)
        self.assertFalse(record['successful'])
        self.assertIn('crashed (exit 7)', record['issue'])
        hang = self.module('hang', 'import threading; threading.Event().wait()\n')
        record = self.run_module(hang, timeout=.2)
        self.assertFalse(record['successful'])
        self.assertIn('timed out', record['issue'])

    def test_discovery_pattern_and_import_error_exit_status(self):
        self.module('test_good', 'import unittest\nclass Tests(unittest.TestCase):\n'
                    '    def test_ok(self): pass\n')
        self.module('test_bad', 'raise RuntimeError("broken import")\n')
        cache = self.directory / 'durations.json'
        args = ['-j', '1', '-s', str(self.directory), '-t', str(self.directory)]
        with mock.patch.object(runner, 'CACHE', cache), redirect_stdout(io.StringIO()):
            self.assertEqual(runner.main(args + ['-p', 'test_good.py']), 0)
            with mock.patch('sys.stderr', new_callable=io.StringIO) as stderr:
                self.assertEqual(runner.main(args), 1)
                self.assertIn('broken import', stderr.getvalue())
        self.assertEqual(set(json.loads(cache.read_text())), {'test_good'})

    def test_cache_schedules_longest_first_and_summary_sorts_names(self):
        cache = self.directory / 'durations.json'
        cache.write_text(json.dumps({'alpha': 1, 'beta': 9, 'gamma': 4}))
        calls = []
        def run(name, *args):
            calls.append(name)
            return dict(module=name, tests=1, failures=0, errors=0, skips=0,
                        duration=1, successful=True, issue=None, output='', tracebacks=[])
        with mock.patch.object(runner, 'CACHE', cache), \
                mock.patch.object(runner.Discovery, 'discover'), \
                mock.patch.object(runner, 'run_module', side_effect=run), \
                redirect_stdout(io.StringIO()) as output:
            original = runner.Discovery.__init__
            def initialize(loader):
                original(loader)
                loader.modules = {'gamma', 'alpha', 'beta'}
            with mock.patch.object(runner.Discovery, '__init__', initialize):
                self.assertEqual(runner.main(['-j', '1']), 0)
        self.assertEqual(calls, ['beta', 'gamma', 'alpha'])
        summary = output.getvalue()
        self.assertLess(summary.index('alpha: tests='), summary.index('beta: tests='))
        self.assertLess(summary.index('beta: tests='), summary.index('gamma: tests='))


if __name__ == '__main__':
    unittest.main()
