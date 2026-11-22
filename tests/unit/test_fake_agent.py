"""Offline Cursor byte shapes, argument boundaries and model-free failures."""
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / 'tests/fixtures/fake-cli/agent'
FIXTURES = FAKE.parent / 'cursor'


class FakeAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='fake-agent-')
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.home = self.directory / 'home'
        self.home.mkdir()
        self.calls = self.directory / 'calls.jsonl'
        self.env = dict(os.environ, HOME=str(self.home), CURSOR_CONFIG_DIR=str(self.home / 'cursor'),
                        ORCEXE_STATE_DIR=str(self.home / 'state'), ORCEXE_FAKE_CALLS=str(self.calls))

    def invoke(self, argv, scenario='success', **extra):
        return subprocess.run([sys.executable, '-B', str(FAKE), *argv], cwd=self.directory,
            env=dict(self.env, ORCEXE_FAKE_SCENARIO=scenario, **extra), stdin=subprocess.DEVNULL,
            capture_output=True, text=True, timeout=10)

    def test_measured_bytes_and_shapes(self):
        probes = [(['--version'], 'version.txt', 1),
                  (['status', '--format', 'json'], 'status.json.txt', 13),
                  (['about', '--format', 'json'], 'about.json.txt', 14),
                  (['models'], 'models.txt', 284),
                  (['--list-models'], 'models.txt', 284), (['--help'], 'help.txt', None)]
        for args, name, count in probes:
            with self.subTest(args=args):
                observed = self.invoke(args)
                expected = (FIXTURES / name).read_text()
                self.assertEqual((observed.returncode, observed.stderr), (0, ''))
                self.assertEqual(observed.stdout, expected)
                self.assertTrue(observed.stdout.endswith('\n'))
                if count:
                    self.assertEqual(len(observed.stdout.splitlines()), count)
        models = self.invoke(['models']).stdout.splitlines()
        self.assertEqual(models[:2], ['Available models', ''])
        self.assertEqual(models[-2], '')
        self.assertTrue(models[-1].startswith('Tip: '))
        self.assertEqual(sum(' - ' in line for line in models), 280)
        for name in ('status.json.txt', 'about.json.txt'):
            text = (FIXTURES / name).read_text()
            self.assertEqual(json.dumps(json.loads(text), indent=2) + '\n', text)
            self.assertIn('fake-user@example.invalid', text)
        unknown = self.invoke(['--bogus-flag'])
        self.assertEqual((unknown.returncode, unknown.stdout, unknown.stderr),
                         (1, '', (FIXTURES / 'unknown-flag.stderr.txt').read_text()))
        self.assertFalse(any(json.loads(c)['model_call'] for c in self.calls.read_text().splitlines()))

    def test_missing_auth_exit_zero_and_invalid_model_stdout(self):
        auth = self.invoke(['status', '--format', 'json'], 'auth-missing')
        self.assertEqual(auth.returncode, 0)
        self.assertIs(json.loads(auth.stdout)['isAuthenticated'], False)
        args = ['-p', 'Read brief', '--output-format', 'stream-json', '--model', 'missing-model']
        invalid = self.invoke(args, 'invalid-model')
        self.assertEqual((invalid.returncode, invalid.stderr), (1, ''))
        self.assertTrue(invalid.stdout.startswith('Cannot use this model: missing-model. Available models: auto, '))
        self.assertFalse(any(json.loads(c)['model_call'] for c in self.calls.read_text().splitlines()))
        self.assertEqual(self.invoke(['-p', 'Read brief'], 'auth-missing').returncode, 1)

    def test_prompt_and_single_path_add_dir_and_strict_options(self):
        args = ['-p', 'Read brief', '--output-format', 'stream-json', '--trust', '--workspace',
                str(self.directory), '--model', 'composer-2.5', '--add-dir', str(self.home)]
        good = self.invoke(args)
        self.assertEqual(good.returncode, 0, good.stderr)
        events = [json.loads(line) for line in good.stdout.splitlines()]
        self.assertEqual(events[0]['model'], 'Composer 2.5')
        self.assertEqual(events[-1]['result'], 'OK\nORCEXE_EXIT: DONE')
        self.assertTrue(any(e['type'] == 'thinking' for e in events))
        for argv in (args + ['--bogus-flag'], args + ['--add-dir'], args + ['extra'],
                     args + ['--mode', 'invalid'], args + ['--sandbox', 'invalid']):
            self.assertEqual(self.invoke(argv).returncode, 1)
        self.assertEqual(self.invoke(['-p', 'Read brief'], 'untrusted').returncode, 1)
        self.assertEqual(self.invoke(args, 'untrusted').returncode, 0)
        shell = self.invoke(args, 'shell-blocked')
        self.assertEqual(shell.returncode, 1)
        self.assertTrue(json.loads(shell.stdout.splitlines()[-1])['is_error'])
        self.assertEqual(self.invoke(args + ['--force'], 'shell-blocked').returncode, 0)
        self.assertEqual(self.invoke(args, 'quota-suspected').returncode, 1)

    @unittest.skipIf(os.name == 'nt', 'Measured POSIX absolute paths')
    def test_project_directories_match_two_real_slug_samples(self):
        # Exercise layout creation without making the measured /Users/u paths.
        with mock.patch.dict(os.environ, self.env), mock.patch.object(sys, 'path', sys.path[:]):
            create_projects = runpy.run_path(str(FAKE))['create_projects']
            create_projects(dict(trust=True,
                workspace='/Users/u/repos/orcexe-wt/do/do-20261117t200438-809b-cursor',
                add_dirs=['/Users/u/repos/orcexe/.orcexe/runs/do-20261117T200438-809b-r1']))
        projects = Path(self.env['CURSOR_CONFIG_DIR']) / 'projects'
        self.assertEqual({path.name for path in projects.iterdir()}, {
            'Users-u-repos-orcexe-wt-do-do-20261117t200438-809b-cursor',
            'Users-u-repos-orcexe-orcexe-runs-do-20261117T200438-809b-r1',
        })

    def test_edit_hook_and_stream_provenance(self):
        target = self.directory / 'edit.txt'
        target.write_text('before')
        args = ['-p', 'Edit', '--output-format', 'stream-json', '--trust', '--model', 'composer-2.5']
        result = self.invoke(args, 'write', ORCEXE_FAKE_EDIT_FILE='edit.txt', ORCEXE_FAKE_EDIT_TEXT='after')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(target.read_text(), 'beforeafter')
        self.invoke(args + ['--mode', 'plan'], 'write', ORCEXE_FAKE_EDIT_FILE='edit.txt', ORCEXE_FAKE_EDIT_TEXT='never')
        self.assertEqual(target.read_text(), 'beforeafter')
        for path in (FAKE.parent / 'streams').glob('cursor-*.jsonl'):
            self.assertEqual(json.loads(path.read_text().splitlines()[0])['_fake'],
                             'measured 2026-11-14 (agent 2026.11.13-eacade1)')

    def test_model_persistence_uses_measured_pretty_json(self):
        config = Path(self.env['CURSOR_CONFIG_DIR']) / 'cli-config.json'
        config.parent.mkdir()
        config.write_text('{"unrelated": 1, "fakeLastRun": "before"}')
        args = ['-p', 'Read brief', '--output-format', 'stream-json', '--model', 'composer-2.5']
        result = self.invoke(args)
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {'unrelated': 1, 'fakeLastRun': 'updated',
                    **json.loads((FIXTURES / 'model-selection.json').read_text())}
        self.assertEqual(json.loads(config.read_text()), expected)
        self.assertEqual(config.read_text(), json.dumps(expected, indent=2) + '\n')
        before = config.read_bytes()
        self.invoke(['models'])
        self.assertEqual(config.read_bytes(), before)

    def test_sandbox_shell_emits_success_without_running_commands(self):
        result = self.invoke(['-p', 'Read brief', '--output-format', 'stream-json',
                              '--model', 'composer-2.5', '--sandbox', 'enabled'], 'shell')
        self.assertEqual(result.returncode, 0, result.stderr)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        calls = [event['shellToolCall'] for event in events
                 if event.get('type') == 'tool_call' and event.get('subtype') == 'completed']
        self.assertEqual(len(calls), 7)
        self.assertTrue(all(isinstance(call['args']['command'], str) for call in calls))
        self.assertTrue(all(call['result']['success']['exitCode'] == 0 for call in calls))
        self.assertFalse(events[-1]['is_error'])
        self.assertEqual(events[-1]['subtype'], 'success')
