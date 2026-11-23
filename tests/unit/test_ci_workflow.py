"""Validate the checked-in Actions workflows without third-party YAML tools."""

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
        # T63b: explicit jobs; 3.9 only where the image provides it (setup-python
        # cannot find 3.9 on ubuntu-latest 24.04), macOS 3.9 is the system Python.
        self.assertEqual(set(matrix), {'include'})
        self.assertEqual([(e['os'], e['python'], e['experimental'], e['system_python'])
                          for e in matrix['include']], [
            ('ubuntu-22.04', '3.9', False, False),
            ('ubuntu-latest', '3.13', False, False),
            ('macos-latest', '3.9', False, True),
            ('macos-latest', '3.13', False, False),
            ('windows-latest', '3.9', True, False),
            ('windows-latest', '3.13', True, False)])
        for entry in matrix['include']:
            self.assertEqual(set(entry), {'os', 'python', 'experimental', 'system_python'})
            self.assertIs(entry['experimental'], entry['os'] == 'windows-latest')
            # Only macOS images ship /usr/bin/python3 3.9 (Command Line Tools).
            self.assertIs(entry['system_python'],
                          entry['os'].startswith('macos') and entry['python'] == '3.9')
        self.assertEqual(job['env'], {
            'PY': "${{ matrix.system_python && '/usr/bin/python3' || 'python' }}"})
        self.assertEqual(job['steps'][1]['if'], '${{ !matrix.system_python }}')
        self.assertEqual(job['steps'][1]['with']['python-version'], '${{ matrix.python }}')
        self.assertEqual([step['run'] for step in job['steps'] if 'run' in step], [
            '${{ env.PY }} -c "import sys; v = \'%d.%d\' % sys.version_info[:2]; '
            'print(sys.version); assert v == \'${{ matrix.python }}\', v"',
            '${{ env.PY }} scripts/check_marketplace.py',
            '${{ env.PY }} tests/tools/run_tests.py'])

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
        # 3.9 is in the ubuntu-22.04 toolcache, not ubuntu-latest (T63b).
        self.assertEqual(job['runs-on'], 'ubuntu-22.04')
        self.assertEqual(job['timeout-minutes'], 30)
        self.assertEqual(job['steps'][1]['with']['python-version'], '3.9')
        self.assertEqual(job['steps'][2]['run'],
                         'ORCEXE_SLOW_TESTS=1 python tests/tools/run_tests.py')


if __name__ == '__main__':
    unittest.main()
