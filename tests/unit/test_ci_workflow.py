"""Validate the checked-in Actions workflows without third-party YAML tools."""

from itertools import product
from pathlib import Path
import re
import sys
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'plugins' / 'orcexe' / 'src'))
from orcexe import miniyaml


class CIWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            name: (ROOT / '.github' / 'workflows' / name).read_text(encoding='utf-8')
            for name in ('ci.yml', 'slow.yml')
        }
        cls.workflows = {name: miniyaml.loads(source)
                         for name, source in cls.sources.items()}

    def test_workflow_safety(self):
        allowed = {
            'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1': 'v7.0.1',
            'actions/setup-python@6d4fd22b20465ccc0f7354476588e8df7c9ca0e0': 'v7.1.0',
        }
        for name, workflow in self.workflows.items():
            with self.subTest(workflow=name):
                self.assertEqual(workflow['permissions'], {'contents': 'read'})
                self.assertIs(workflow['concurrency']['cancel-in-progress'], True)
                self.assertEqual(workflow['concurrency']['group'],
                                 ('ci-' if name == 'ci.yml' else 'slow-') + '${{ github.ref }}')
                self.assertEqual(workflow['env']['PYTHONDONTWRITEBYTECODE'], '1')
                self.assertNotRegex(self.sources[name], r'secrets\s*\.')
                for job in workflow['jobs'].values():
                    self.assertGreater(job['timeout-minutes'], 0)
                    actions = []
                    for step in job['steps']:
                        if 'uses' in step:
                            action = step['uses']
                            self.assertRegex(action, r'^[^@\s]+@[0-9a-fA-F]{40}$')
                            self.assertIn(action, allowed)
                            self.assertRegex(
                                self.sources[name],
                                re.escape(action) + r'\s+# ' + re.escape(allowed[action]) + r'\s*\n')
                            actions.append(action.split('@')[0])
                    self.assertEqual(actions, ['actions/checkout', 'actions/setup-python'])

    def test_matrix_and_commands(self):
        job = self.workflows['ci.yml']['jobs']['test']
        self.assertEqual(job['timeout-minutes'], 15)
        self.assertIs(job['strategy']['fail-fast'], False)
        self.assertEqual(job['runs-on'], '${{ matrix.os }}')
        self.assertEqual(job['continue-on-error'], '${{ matrix.experimental }}')
        matrix = job['strategy']['matrix']
        self.assertEqual(matrix['os'], ['ubuntu-latest', 'macos-latest', 'windows-latest'])
        self.assertEqual(matrix['python'], ['3.9', '3.13'])
        self.assertEqual(set(matrix), {'os', 'python', 'include'})
        # Include adds flags to existing combinations, never an extra job.
        entries = [dict(os=os, python=python)
                   for os, python in product(matrix['os'], matrix['python'])]
        for addition in matrix['include']:
            matches = [entry for entry in entries if entry['os'] == addition['os']]
            self.assertEqual(len(matches), 2)
            self.assertEqual(set(addition), {'os', 'experimental'})
            for entry in matches:
                entry.update(addition)
        self.assertEqual(len(entries), 6)
        for entry in entries:
            self.assertIs(entry['experimental'], entry['os'] == 'windows-latest')
        self.assertEqual([step['run'] for step in job['steps'] if 'run' in step],
                         ['python scripts/check_marketplace.py',
                          'python tests/tools/run_tests.py'])
        self.assertEqual(job['steps'][1]['with']['python-version'], '${{ matrix.python }}')

    def test_triggers(self):
        triggers = self.workflows['ci.yml']['on']
        self.assertEqual(set(triggers), {'pull_request', 'push', 'workflow_dispatch'})
        self.assertNotIn('branches', triggers['pull_request'])
        self.assertEqual(triggers['push']['branches'], ['main'])
        # GitHub applies these in order: ignore Markdown, then restore plugin paths.
        for event in ('pull_request', 'push'):
            self.assertEqual(triggers[event]['paths'], [
                '**', '!**/*.md', 'plugins/orcexe/skills/**', 'plugins/orcexe/commands/**'])
        slow = self.workflows['slow.yml']
        self.assertEqual(set(slow['on']), {'workflow_dispatch', 'schedule', 'pull_request'})
        self.assertEqual(slow['on']['schedule'], [{'cron': '0 3 * * 1'}])
        self.assertEqual(slow['on']['pull_request']['paths'], [
            'plugins/orcexe/src/orcexe/redact*.py',
            'plugins/orcexe/src/orcexe/classify.py',
            'plugins/orcexe/src/orcexe/miniyaml.py'])
        job = slow['jobs']['slow']
        self.assertEqual(job['runs-on'], 'ubuntu-latest')
        self.assertEqual(job['timeout-minutes'], 30)
        self.assertEqual(job['steps'][1]['with']['python-version'], '3.9')
        self.assertEqual(job['steps'][2]['run'],
                         'ORCEXE_SLOW_TESTS=1 python tests/tools/run_tests.py')


if __name__ == '__main__':
    unittest.main()
