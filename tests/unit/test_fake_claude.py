"""Measured fake argv and stream parity, without locating a real AI CLI."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tests.unit._timing import scaled
from tests.contract.test_manifest_parity import parser_flags
from orcexe.manifest import load_manifest

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / 'tests/fixtures/fake-cli/claude'
MANIFEST = ROOT / 'plugins/orcexe/adapters/claude.yaml'


class FakeClaudeTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix='fake-claude-')
        self.addCleanup(tmp.cleanup)
        self.directory = Path(tmp.name).resolve()
        self.calls = self.directory / 'calls.jsonl'
        self.env = dict(PATH=os.environ.get('PATH', os.defpath), HOME=str(self.directory),
                        CLAUDE_CONFIG_DIR=str(self.directory / 'claude'),
                        CODEX_HOME=str(self.directory / 'codex'), ORCEXE_FAKE_CALLS=str(self.calls))
        for key in ('SystemRoot', 'SYSTEMROOT', 'WINDIR'):
            if key in os.environ:
                self.env[key] = os.environ[key]
        self.manifest = load_manifest(MANIFEST)

    def invoke(self, argv, scenario='success', *, stdin_text=None, **env):
        return subprocess.run([sys.executable, '-B', str(FAKE), *argv], env=dict(self.env,
            ORCEXE_FAKE_SCENARIO=scenario, **env), cwd=str(self.directory),
            capture_output=True, text=True, timeout=scaled(3),
            **({'stdin': subprocess.DEVNULL} if stdin_text is None else {'input': stdin_text}))

    def argv(self, profile='review'):
        return self.manifest.build_argv(profile, run_dir=str(self.directory),
            model='claude-sonnet-5-5', effort='medium', prompt='Do the fixture task')[1:]

    def events(self, result):
        return [json.loads(line) for line in result.stdout.splitlines()]

    def test_manifest_flag_inventory_and_profiles(self):
        produced = set()
        for profile in self.manifest.data['exec']['profiles']:
            argv = self.argv(profile)
            produced.update(token for token in argv if token.startswith('-'))
            result = self.invoke(argv)
            self.assertEqual(result.returncode, 0, result.stderr)
            events = self.events(result)
            self.assertEqual([event['type'] for event in events],
                ['system', 'system', 'assistant', 'rate_limit_event', 'system', 'result'])
            self.assertEqual(events[-1]['terminal_reason'], 'completed')
            self.assertEqual(events[0]['permissionMode'], 'acceptEdits' if profile == 'write' else 'plan')
        self.assertEqual(produced | {'--allowedTools', '--allowed-tools', '--disallowed-tools'},
                         parser_flags(FAKE))
        bad = self.invoke([*self.argv(), '--bogus-flag'])
        self.assertEqual((bad.returncode, bad.stderr), (1, "error: unknown option '--bogus-flag'\n"))
        self.assertFalse(json.loads(self.calls.read_text().splitlines()[-1])['model_call'])

    def assert_missing_input(self, result):
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        self.assertEqual(result.stderr, 'Error: Input must be provided either through stdin '
                         'or as a prompt argument when using --print\n')
        self.assertFalse(json.loads(self.calls.read_text().splitlines()[-1])['model_call'])

    def test_old_prompt_order_fails_and_positional_first_works(self):
        for profile in self.manifest.data['exec']['profiles']:
            with self.subTest(profile=profile):
                argv = self.argv(profile)
                self.assertEqual(argv[:2], ['-p', 'Do the fixture task'])
                old = list(argv)
                old.remove('Do the fixture task')
                old.insert(old.index('--add-dir') + 2, 'Do the fixture task')
                self.assert_missing_input(self.invoke(old))
                result = self.invoke(argv)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertTrue(json.loads(self.calls.read_text().splitlines()[-1])['model_call'])

    def test_variadic_options_consume_until_next_flag(self):
        for flag in ('--add-dir', '--allowedTools', '--allowed-tools',
                     '--disallowedTools', '--disallowed-tools'):
            with self.subTest(flag=flag):
                argv = ['-p', '--output-format', 'stream-json', '--verbose',
                        flag, 'first', 'second', 'swallowed prompt', '--permission-mode', 'plan']
                self.assert_missing_input(self.invoke(argv))
                # A positional prompt before the variadic option survives, and
                # --permission-mode stops consumption of that option's values.
                result = self.invoke([argv[0], 'prompt', *argv[1:]])
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.events(result)[0]['permissionMode'], 'plan')

    def test_print_without_positional_prompt_uses_stdin(self):
        argv = ['-p', '--output-format', 'stream-json', '--verbose', '--add-dir', 'directory']
        for stdin_text in (None, ''):
            with self.subTest(stdin_text=stdin_text):
                self.assert_missing_input(self.invoke(argv, stdin_text=stdin_text))
        result = self.invoke(argv, stdin_text='Task from stdin\n')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(self.calls.read_text().splitlines()[-1])['model_call'])

    def test_auth_version_and_invalid_model(self):
        self.assertEqual(self.invoke(['--version']).stdout, '2.1.310 (Claude Code)\n')
        auth = self.invoke(['auth', 'status', '--json'])
        self.assertEqual(auth.returncode, 0)
        self.assertEqual(auth.stdout, json.dumps(json.loads(auth.stdout), indent=2) + '\n')
        self.assertEqual(len(auth.stdout.splitlines()), 12)
        self.assertEqual(list(json.loads(auth.stdout)), ['loggedIn', 'authMethod', 'apiProvider',
            'email', 'orgId', 'orgName', 'subscriptionType', 'analyticsDisabled',
            'projectsDirectory', 'configDirectory'])
        self.assertEqual(set(json.loads(auth.stdout)), {'analyticsDisabled', 'apiProvider',
            'authMethod', 'configDirectory', 'email', 'loggedIn', 'orgId', 'orgName',
            'projectsDirectory', 'subscriptionType'})
        missing = self.invoke(['auth', 'status', '--json'], 'auth-missing')
        self.assertEqual(missing.returncode, 1)
        self.assertIs(json.loads(missing.stdout)['loggedIn'], False)
        invalid = self.invoke(self.argv(), 'invalid-model')
        self.assertEqual((invalid.returncode, invalid.stderr), (1, ''))
        self.assertIn("There's an issue with the selected model", invalid.stdout)

    def test_compact_auth_is_opt_in(self):
        pretty = self.invoke(['auth', 'status', '--json'])
        compact = self.invoke(['auth', 'status', '--json'], ORCEXE_FAKE_AUTH_COMPACT='1')
        self.assertEqual(compact.returncode, 0)
        self.assertEqual(compact.stdout, json.dumps(json.loads(pretty.stdout)) + '\n')
        self.assertEqual(len(compact.stdout.splitlines()), 1)
        disabled = self.invoke(['auth', 'status', '--json'], ORCEXE_FAKE_AUTH_COMPACT='0')
        self.assertEqual(disabled.stdout, pretty.stdout)
        self.assertFalse(any(json.loads(line)['model_call'] for line in self.calls.read_text().splitlines()))

    def test_hooks_quota_write_and_default_off_edit(self):
        hooks = self.events(self.invoke(self.argv(), 'hooks'))
        self.assertEqual([event['subtype'] for event in hooks[:3]], ['hook_started', 'hook_response', 'init'])
        quota = self.invoke(self.argv(), 'quota')
        self.assertEqual(quota.returncode, 1)
        self.assertTrue(self.events(quota)[-1]['is_error'])
        self.invoke(self.argv('write'))
        target = self.directory / 'orcexe-fake-output.txt'
        self.assertFalse(target.exists())
        self.invoke(self.argv('review'), 'write')
        self.assertFalse(target.exists())
        self.invoke(self.argv('write'), 'write')
        self.assertEqual(target.read_text(), 'OK\n')
        self.invoke(self.argv('write'), ORCEXE_FAKE_EDIT_FILE=target.name, ORCEXE_FAKE_EDIT_TEXT='edited\n')
        self.assertEqual(target.read_text(), 'OK\nedited\n')
        # The fake reproduces Claude's silent ignore; the adapter must preflight.
        argv = self.argv()
        argv[argv.index('--add-dir') + 1] = str(self.directory / 'absent')
        self.assertEqual(self.invoke(argv).returncode, 0)

    def test_fixture_provenance(self):
        for path in FAKE.parent.joinpath('streams').glob('claude-*.jsonl'):
            header = json.loads(path.read_text().splitlines()[0])
            self.assertIn('measured 2026-11-14 (claude 2.1.310)', header['_fake'])
