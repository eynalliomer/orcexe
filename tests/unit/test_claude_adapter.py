"""Claude-specific probes, extraction, preflight and subscription boundaries."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from unittest import mock

from tests.unit.test_gitutil import GitFixture, ROOT
from tests.unit._timing import scaled
from orcexe.adapters import ClaudeCodeAdapter, RunRequest, validate_run_result
from orcexe.manifest import Manifest, ManifestError
from orcexe.miniyaml import load_path
from orcexe import proc

FAKE = ROOT / 'tests/fixtures/fake-cli' / ('claude.cmd' if os.name == 'nt' else 'claude')
MANIFEST = ROOT / 'plugins/orcexe/adapters/claude.yaml'


class ClaudeAdapterTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.work = self.linked()
        self.brief = self.directory / 'brief.md'
        self.brief.write_text('Do the fixture task')
        self.calls = self.directory / 'calls.jsonl'
        self.env.update(ORCEXE_BIN_CLAUDE_CODE=str(FAKE), ORCEXE_FAKE_CALLS=str(self.calls),
            CLAUDE_CONFIG_DIR=str(self.home / 'claude'), ORCEXE_STATE_DIR=str(self.home / 'state'),
            ORCEXE_FAKE_PYTHON=sys.executable)

    def adapter(self, scenario='success', manifest=None, **env):
        current = dict(self.env, ORCEXE_FAKE_SCENARIO=scenario, **env)
        return ClaudeCodeAdapter(manifest, env=current, allow_extra=tuple(current),
            detect_timeout_s=scaled(3), kill_grace_s=scaled(.1))

    def request(self, name='run', profile='review'):
        return RunRequest(name, 'T12', 'claude-code', 'claude-sonnet-5-5', 'medium', profile,
            str(self.work), str(self.brief), str(self.directory / name), timeout_s=scaled(4), stall_s=scaled(3))

    def test_exact_argv_order_for_both_profiles(self):
        for profile, extra in (
                ('write', ['--permission-mode', 'acceptEdits', '--disallowedTools', 'Bash,WebFetch,WebSearch']),
                ('review', ['--permission-mode', 'plan'])):
            with self.subTest(profile=profile):
                req = self.request(profile, profile)
                result = self.adapter().run(req)
                self.assertEqual(result['status'], 'completed')
                calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                argv = [call['argv'] for call in calls if call['model_call']][-1]
                prompt = 'Read the task brief at {} and carry it out. Follow its rules exactly.'.format(
                    Path(req.run_dir) / 'brief.md')
                self.assertEqual(argv, ['-p', prompt, '--output-format', 'stream-json', '--verbose',
                    '--model', req.model, '--effort', req.effort, '--no-session-persistence',
                    '--strict-mcp-config', '--include-hook-events', '--add-dir', req.run_dir, *extra])
        self.assertEqual(self.adapter().manifest.evidence('exec.argv'), 'Y')

    def test_probe_allowlist_and_honest_models(self):
        adapter = self.adapter()
        for compact in ('0', '1'):
            for scenario, state in (('success', 'ok'), ('auth-missing', 'missing')):
                with self.subTest(compact=compact, scenario=scenario):
                    auth = self.adapter(scenario, ORCEXE_FAKE_AUTH_COMPACT=compact).auth_status()
                    self.assertEqual(auth, dict(state=state, method='claude.ai', plan='pro', source='probe'))
                    encoded = json.dumps(auth)
                    for private in ('claude-fake@example.invalid', 'fake-claude-org-id', 'fake-claude-org-name',
                                    '/synthetic/', 'email', 'orgId', 'orgName', 'configDirectory',
                                    'projectsDirectory', 'analyticsDisabled', 'apiProvider'):
                        self.assertNotIn(private, encoded)
        self.assertEqual(adapter.list_models(), dict(models=None, source='none', error='no model-free model list in claude CLI'))
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertFalse(any(call['model_call'] for call in calls))

    def test_auth_whole_document_validation_and_byte_limit(self):
        adapter = self.adapter()
        runner = proc.run
        payload = self.directory / 'auth-stdout.json'
        valid = '{"loggedIn": true, "authMethod": "claude.ai", "subscriptionType": "pro"}'
        ok = dict(state='ok', method='claude.ai', plan='pro', source='probe')
        unknown = dict(state='unknown', method='unknown', plan=None, source='probe')
        limit = 64 * 1024

        def injected(argv, **options):
            self.assertEqual(argv[1:], ['auth', 'status', '--json'])
            # Exercise the real bounded process reader with synthetic stdout.
            return runner([sys.executable, '-B', '-c',
                'import sys; sys.stdout.buffer.write(open(sys.argv[1], "rb").read())',
                str(payload)], **options)

        cases = [
            ('exact-limit', valid + ' ' * (limit - len(valid)), ok, 1),
            ('exact-limit-newline', valid + ' ' * (limit - len(valid) - 1) + '\n', ok, 1),
            ('malformed', '{\n"loggedIn": true,\n', unknown, 1),
            ('non-object', '[{"loggedIn": true}]', unknown, 1),
            ('two-documents', valid + '\n' + valid, unknown, 1),
            ('trailing-garbage', valid + '\ninvalid', unknown, 1),
            ('wrong-types', '{"loggedIn": 1, "authMethod": {"email": "private"}, '
                            '"subscriptionType": ["private"]}', unknown, 1),
            ('oversized-line', valid + ' ' * limit, unknown, 0),
            ('oversized-document', valid + '\n' + (' ' * 1024 + '\n') * 64, unknown, 0),
            ('oversized-blank-lines', valid + '\n' * limit, unknown, 0),
            ('oversized-utf8', '{"loggedIn": true, "email": "' + '\u00e9' * (limit // 2) + '"}', unknown, 0),
        ]
        loads = json.loads
        for name, text, expected, parses in cases:
            with self.subTest(name=name):
                payload.write_text(text, encoding='utf-8')
                with mock.patch.object(proc, 'run', side_effect=injected), \
                        mock.patch('orcexe.adapters.claude_code.json.loads', wraps=loads) as decode:
                    self.assertEqual(adapter.auth_status(), expected)
                    self.assertEqual(decode.call_count, parses)

    def test_child_environment_never_switches_provider(self):
        secrets = dict(ANTHROPIC_API_KEY='secret', ANTHROPIC_AUTH_TOKEN='secret',
            ANTHROPIC_BASE_URL='https://invalid.example', CLAUDE_CODE_USE_BEDROCK='1',
            CLAUDE_CODE_USE_VERTEX='1', CLAUDE_CODE_USE_FOUNDRY='1')
        with mock.patch.dict(os.environ, secrets):
            adapter = self.adapter(**secrets)
            env = adapter._environment()
            self.assertFalse(set(secrets) & set(env))
            self.assertEqual(env['CLAUDE_CONFIG_DIR'], self.env['CLAUDE_CONFIG_DIR'])
            runner = proc.run
            seen = []
            def inspected(argv, **options):
                seen.append(options['env'])
                self.assertFalse(set(secrets) & set(options['env']))
                return runner(argv, **options)
            with mock.patch.object(proc, 'run', side_effect=inspected):
                adapter.auth_status()
                self.assertEqual(adapter.run(self.request())['status'], 'completed')
            self.assertTrue(seen)

    def test_usage_list_cost_and_quota_cache_round_trip(self):
        adapter = self.adapter()
        self.assertEqual(adapter.quota()['source'], 'none')
        with mock.patch('orcexe.adapters.claude_code.time.time', return_value=1000):
            result = adapter.run(self.request())
        self.assertEqual(result['usage'], dict(input=10, cached_read=2, cache_write=3, output=4, reasoning=None))
        self.assertEqual(result['cost'], dict(list_usd=.012, basis='tool'))
        self.assertEqual(result['model_actual'], 'claude-sonnet-5-5')
        self.assertEqual(Path(result['final_text_path']).read_text(), 'OK\nORCEXE_EXIT: DONE')
        validate_run_result(result, run_dir=self.request().run_dir)
        fresh = self.adapter()
        with mock.patch('orcexe.adapters.claude_code.time.time', return_value=1012):
            quota = fresh.quota()
        self.assertEqual(quota['age_s'], 12)
        self.assertIs(quota['allowed'], True)
        self.assertEqual([w['used_pct'] for w in quota['windows']], [44, 74])
        self.assertEqual(quota['windows'], result['quota_after']['windows'])
        rejected = self.adapter('quota')
        self.assertEqual(rejected.run(self.request('quota'))['status'], 'quota')
        self.assertIs(fresh.quota()['allowed'], False)
        cache = self.home / 'state/quota-claude.json'
        data = json.loads(cache.read_text())
        data['rate_limit_info']['email'] = 'should-not-return'
        cache.write_text(json.dumps(data))
        self.assertNotIn('should-not-return', json.dumps(fresh.quota()))
        cache.write_text('[]')
        self.assertEqual(fresh.quota()['source'], 'none')

    def test_add_dir_validation_precedes_every_process(self):
        for kind in ('missing', 'file', 'relative'):
            raw = load_path(MANIFEST)
            target = self.directory / ('grant-' + kind)
            if kind == 'file':
                target.write_text('not a directory')
            raw['exec']['profiles']['review']['extra'] += ['--add-dir', 'relative' if kind == 'relative' else str(target)]
            adapter = self.adapter(manifest=Manifest(raw))
            with mock.patch.object(proc, 'run', side_effect=AssertionError('process before preflight')):
                result = adapter.run(self.request(kind))
            self.assertEqual(result['status'], 'blocked')
            self.assertIn('add_dir_invalid', result['warnings'])
            validate_run_result(result)
        self.assertFalse(self.calls.exists())

    def test_forbidden_argv(self):
        for extra in (['--dangerously-skip-permissions'], ['--permission-mode', 'bypassPermissions'], ['--bare'], ['--plugin-url', 'https://invalid.example']):
            raw = load_path(MANIFEST)
            raw['exec']['profiles']['write']['extra'] += extra
            with self.assertRaises(ManifestError):
                Manifest(raw)

    def test_event_types_and_terminal_evidence_not_order(self):
        adapter = self.adapter()
        success = dict(type='result', subtype='success', is_error=False, terminal_reason='completed', result='OK')
        ended = proc.ProcResult('exited', 0, .01, 1, 1, 0, '', False, None)
        for events, expected in (([success, {'type': 'system', 'subtype': 'post_turn_summary'}], ('completed', [])),
            ([{'type': 'assistant'}], ('failed', ['final_event_missing'])),
            ([dict(success, is_error=True)], ('failed', [])),
            ([dict(success, terminal_reason='unknown')], ('failed', [])),
            ([{'type': 'rate_limit_event', 'rate_limit_info': {'status': 'allowed'}}, success], ('completed', [])),
            ([success, {'type': 'rate_limit_event', 'rate_limit_info': {'status': 'rejected'}}], ('quota', []))):
            self.assertEqual(adapter._classify(dict(events=events, counts={}), ended), expected)
        for text in ('Please run /login', 'Invalid API key', 'authentication_failed', 'HTTP 401'):
            for carrier in ('stdout', 'stderr', 'result'):
                events = [{'_raw': text}] if carrier == 'stdout' else [dict(success, result=text)] if carrier == 'result' else []
                observed = replace(ended, returncode=1, stderr_tail=text if carrier == 'stderr' else '')
                self.assertEqual(adapter._classify(dict(events=events, counts={}), observed), ('auth', []))

    def test_init_survives_long_tool_stream_and_cache_allowlist(self):
        adapter = self.adapter()
        runner = proc.run
        def injected(argv, **options):
            if argv[1] != '-p':
                return runner(argv, **options)
            emit = options['on_line']
            emit(json.dumps(dict(type='system', subtype='init', model='claude-opus-5-5')))
            emit(json.dumps(dict(type='rate_limit_event', rate_limit_info=dict(status='allowed', email='private',
                unifiedWindows={'five_hour': dict(utilization=.2, resetsAt=1800000000, orgId='private')}))))
            for _ in range(40):
                emit(json.dumps(dict(type='assistant', message={'content': []})))
            emit(json.dumps(dict(type='result', subtype='success', is_error=False, terminal_reason='completed', result='OK')))
            # The child can edit every artifact in its --add-dir grant. Neither
            # classification nor extraction may read those edits as evidence.
            directory = Path(argv[argv.index('--add-dir') + 1])
            (directory / 'events.jsonl').write_text(json.dumps(dict(type='rate_limit_event',
                rate_limit_info={'status': 'rejected'})) + '\n')
            (directory / 'result.json').write_text('{"status":"quota"}')
            return proc.ProcResult('exited', 0, .01, 43, 0, 0, '', False, None)
        with mock.patch.object(proc, 'run', side_effect=injected):
            result = adapter.run(self.request())
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['model_actual'], 'claude-opus-5-5')
        self.assertIn('model_mismatch', result['warnings'])
        self.assertNotIn('private', (self.home / 'state/quota-claude.json').read_text())
