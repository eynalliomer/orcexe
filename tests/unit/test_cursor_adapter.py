"""Cursor probes, live slug selection, privacy and pinned execution profiles."""
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
from unittest import mock

from tests.unit.test_gitutil import GitFixture, ROOT
from tests.unit._timing import roomy, run_when_ready, scaled
from orcexe import fsio, proc
from orcexe.adapters import CursorAdapter, RunRequest, validate_run_result
from orcexe.manifest import Manifest, ManifestError
from orcexe.adapters.cursor import _MODEL_KEYS
from orcexe.miniyaml import load_path

FAKE = ROOT / 'tests/fixtures/fake-cli' / ('agent.cmd' if os.name == 'nt' else 'agent')
MANIFEST = ROOT / 'plugins/orcexe/adapters/cursor.yaml'


class CursorAdapterTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.work = self.linked()
        self.brief = self.directory / 'brief.md'
        self.brief.write_text('Do the fixture task')
        self.calls = self.directory / 'calls.jsonl'
        self.env.update(ORCEXE_BIN_CURSOR=str(FAKE), ORCEXE_FAKE_CALLS=str(self.calls),
            ORCEXE_FAKE_PYTHON=sys.executable, CURSOR_CONFIG_DIR=str(self.home / 'cursor'),
            ORCEXE_STATE_DIR=str(self.home / 'state'))

    def adapter(self, scenario='success', manifest=None, **env):
        current = dict(self.env, ORCEXE_FAKE_SCENARIO=scenario, **env)
        return CursorAdapter(manifest, env=current, allow_extra=tuple(current),
            # Version/auth/models probes must finish; see _timing.roomy.
            detect_timeout_s=roomy(), kill_grace_s=scaled(.1))

    def request(self, name='run', profile='review', model='composer-2.5', effort='medium'):
        return RunRequest(name, 'T14', 'cursor', model, effort, profile, str(self.work),
            str(self.brief), str(self.directory / name), timeout_s=roomy(), stall_s=roomy())

    def log(self):
        return [json.loads(c) for c in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_exact_argv_for_both_profiles_and_cost_usage(self):
        for profile, extra in (('write', ['--sandbox', 'enabled']), ('review', ['--mode', 'plan'])):
            with self.subTest(profile=profile):
                req = self.request(profile, profile)
                result = self.adapter().run(req)
                self.assertEqual(result['status'], 'completed')
                self.assertIn('effort_not_applicable', result['warnings'])
                prompt = 'Read the task brief at {} and carry it out. Follow its rules exactly.'.format(Path(req.run_dir) / 'brief' / 'brief.md')
                argv = [c['argv'] for c in self.log() if c['model_call']][-1]
                self.assertEqual(argv, ['-p', prompt, '--output-format', 'stream-json', '--trust',
                    '--workspace', req.workdir, '--model', 'composer-2.5', '--add-dir', str(Path(req.run_dir) / 'brief'), *extra])
                self.assertEqual(result['usage'], dict(input=10, cached_read=2, cache_write=3, output=4, reasoning=None))
                self.assertEqual(result['cost'], dict(list_usd=None, basis='unknown'))
                self.assertEqual(result['model_actual'], 'composer-2.5')
                self.assertNotIn('thinking', Path(result['events_path']).read_text())
                validate_run_result(result, run_dir=req.run_dir)
        self.assertEqual(self.adapter().manifest.evidence('exec.profiles.write.extra'), 'Y')

    def config(self, data):
        path = Path(self.env['CURSOR_CONFIG_DIR']) / 'cli-config.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        path.chmod(0o640)
        return path

    def test_default_model_restored_preserving_other_changes_order_and_mode(self):
        original = json.loads((FAKE.parent / 'cursor/model-selection.json').read_text())
        original['model']['modelId'] = 'default'
        original['selectedModel'] = {'modelId': 'default', 'parameters': []}
        original['modelSelectionHistory'] = ['default']
        original['modelParameters'] = {'default': []}
        original['hasChangedDefaultModel'] = False
        original = dict(zFirst='private config value', **original, fakeLastRun='before', aLast={'theme': 'dark'})
        path = self.config(original)
        analytics = path.with_name('statsig-cache.json')
        analytics.write_bytes(b'analytics cache\n')
        result = self.adapter().run(self.request())
        after = json.loads(path.read_text())
        for key in _MODEL_KEYS:
            self.assertEqual(json.dumps(after[key]), json.dumps(original[key]))
        self.assertEqual(after, dict(original, fakeLastRun='updated'))
        self.assertEqual(list(after), list(original))
        self.assertEqual(path.read_text(), json.dumps(after, ensure_ascii=False, indent=2) + '\n')
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertIn('cursor_default_model_restored', result['warnings'])
        self.assertEqual(json.loads((Path(result['events_path']).parent / 'result.json').read_text()), result)
        self.assertEqual(analytics.read_bytes(), b'analytics cache\n')
        for file in Path(self.request().run_dir).iterdir():
            if file.is_file():
                self.assertNotIn('private config value', file.read_text())

    def test_absent_model_keys_deleted_and_home_fallback(self):
        # Empty CURSOR_CONFIG_DIR follows the same fallback as an unset one.
        self.env['CURSOR_CONFIG_DIR'] = ''
        path = self.home / '.cursor/cli-config.json'
        path.parent.mkdir()
        path.write_text('{"unrelated": 1}\n')
        result = self.adapter().run(self.request())
        self.assertEqual(json.loads(path.read_text()), {'unrelated': 1, 'fakeLastRun': 'updated'})
        self.assertIn('cursor_default_model_restored', result['warnings'])

    def test_missing_and_unreadable_configs_never_written(self):
        path = Path(self.env['CURSOR_CONFIG_DIR']) / 'cli-config.json'
        self.assertEqual(self.adapter().run(self.request('missing'))['status'], 'completed')
        self.assertFalse(path.exists())
        self.assertTrue((path.parent / 'projects').is_dir())
        path.parent.mkdir(exist_ok=True)
        for index, payload in enumerate((b'{private broken JSON', b'[]\n', b'\xff',
                b'{' + b'x' * (256 * 1024))):
            path.write_bytes(payload)
            with mock.patch.object(fsio, 'atomic_write_text', wraps=fsio.atomic_write_text) as write:
                result = self.adapter().run(self.request('bad-' + str(index)))
            self.assertEqual(path.read_bytes(), payload)
            self.assertIn('cursor_config_unreadable', result['warnings'])
            self.assertNotIn('cursor_default_model_restored', result['warnings'])
            self.assertFalse(any(Path(call.args[0]) == path for call in write.call_args_list))

    def test_post_run_unreadable_noop_and_unchanged_config_not_rewritten(self):
        path = self.config({'model': None, 'unrelated': 'keep'})
        original = path.read_bytes()
        runner = proc.run
        def injected(argv, **options):
            observed = runner(argv, **options)
            if '-p' in argv:
                path.write_bytes(b'{unreadable after run')
            return observed
        with mock.patch.object(proc, 'run', side_effect=injected):
            result = self.adapter().run(self.request('post-bad'))
        self.assertIn('cursor_config_unreadable', result['warnings'])
        self.assertEqual(path.read_bytes(), b'{unreadable after run')
        path.write_bytes(original)
        adapter = self.adapter('invalid-model')
        result = adapter.run(self.request('unchanged'))
        self.assertEqual(path.read_bytes(), original)
        self.assertNotIn('cursor_default_model_restored', result['warnings'])

        # A run changing only unrelated state must not trigger a restore write.
        selected = json.loads((FAKE.parent / 'cursor/model-selection.json').read_text())
        self.config(dict(selected, fakeLastRun='before'))
        with mock.patch.object(fsio, 'atomic_write_text', wraps=fsio.atomic_write_text) as write:
            result = self.adapter().run(self.request('same-model'))
        self.assertEqual(json.loads(path.read_text()), dict(selected, fakeLastRun='updated'))
        self.assertNotIn('cursor_default_model_restored', result['warnings'])
        self.assertFalse(any(Path(call.args[0]) == path for call in write.call_args_list))

    def test_config_read_is_bounded_in_bytes(self):
        path = self.config({'model': 'default'})
        payload = path.read_bytes()
        path.write_bytes(payload + b' ' * (256 * 1024 - len(payload)))
        adapter = self.adapter()
        warnings = []
        self.assertEqual(adapter._snapshot_model(path, warnings)[0], {'model': 'default'})
        self.assertEqual(warnings, [])
        path.write_bytes(path.read_bytes() + b' ')
        self.assertIsNone(adapter._snapshot_model(path, warnings))
        self.assertEqual(warnings, ['cursor_config_unreadable'])

    def test_restore_on_failure_timeout_stall_cancel_and_runner_exception(self):
        for outcome in ('failed', 'timeout', 'stalled', 'cancelled', 'exception'):
            with self.subTest(outcome=outcome):
                path = self.config({'unrelated': 42, 'model': None})
                ready = self.directory / ('ready-' + outcome)
                adapter = self.adapter('quota-suspected' if outcome == 'failed' else 'stall',
                    ORCEXE_FAKE_READY_FILE=str(ready))
                req = replace(self.request(outcome), timeout_s=scaled(.3) if outcome == 'timeout' else roomy(),
                              stall_s=scaled(.3) if outcome == 'stalled' else roomy())
                runner = proc.run
                def injected(argv, **options):
                    if '-p' not in argv or outcome == 'failed':
                        return runner(argv, **options)
                    if outcome == 'exception':
                        path.write_text('{"model": "changed", "unrelated": 42, "fakeLastRun": "updated"}')
                        raise RuntimeError('injected runner failure')
                    start = (lambda: adapter.cancel(req.run_id)) if outcome == 'cancelled' else None
                    return run_when_ready(runner, argv, ready.exists, self.addCleanup, start=start, **options)
                with mock.patch.object(proc, 'run', side_effect=injected):
                    if outcome == 'exception':
                        with self.assertRaisesRegex(RuntimeError, 'injected runner failure'):
                            adapter.run(req)
                    else:
                        result = adapter.run(req)
                        self.assertEqual(result['status'], outcome)
                        self.assertIn('cursor_default_model_restored', result['warnings'])
                self.assertEqual(json.loads(path.read_text()),
                                 {'unrelated': 42, 'model': None, 'fakeLastRun': 'updated'})

    def test_sandbox_shell_success_count_and_explicit_refusal(self):
        for scenario, status, calls in (('shell', 'completed', 7), ('shell-blocked', 'failed', 1)):
            adapter = self.adapter(scenario)
            classify = adapter._classify
            with mock.patch.object(adapter, '_classify', wraps=classify) as classify:
                result = adapter.run(self.request(scenario, profile='write'))
            self.assertEqual(result['status'], status)
            self.assertIn('shell_used:{}'.format(calls), result['warnings'])
            self.assertEqual(classify.call_args.args[0]['shell_calls'], calls)
            self.assertEqual('shell_blocked' in result['warnings'], scenario == 'shell-blocked')
            argv = [call['argv'] for call in self.log() if call['model_call']][-1]
            self.assertEqual(argv[-2:], ['--sandbox', 'enabled'])
            self.assertNotIn('--force', argv)

    def test_shell_counter_survives_recent_event_eviction(self):
        runner = proc.run
        def injected(argv, **options):
            if '-p' not in argv:
                return runner(argv, **options)
            emit = options['on_line']
            for _ in range(40):
                for phase in ('started', 'completed'):
                    emit(json.dumps(dict(type='tool_call', subtype=phase,
                        shellToolCall={'args': {'command': 'git diff'},
                                       'result': {'success': {'exitCode': 0}}})))
            for _ in range(40):
                emit(json.dumps(dict(type='assistant', message={'content': 'OK'})))
            emit(json.dumps(dict(type='result', subtype='success', is_error=False, result='ORCEXE_EXIT: DONE')))
            return proc.ProcResult('exited', 0, .01, 0, 0, 0, '', False, None)
        adapter = self.adapter()
        with mock.patch.object(proc, 'run', side_effect=injected), \
                mock.patch.object(adapter, '_classify', wraps=adapter._classify) as classify:
            result = adapter.run(self.request())
        self.assertEqual(result['status'], 'completed')
        self.assertIn('shell_used:40', result['warnings'])
        summary = classify.call_args.args[0]
        self.assertEqual(summary['shell_calls'], 40)
        self.assertFalse(any(e.get('type') == 'tool_call' for e in summary['events']))

    def test_models_markers_effort_grouping_and_live_selection(self):
        adapter = self.adapter()
        models = adapter.list_models()
        self.assertEqual(models['source'], 'cmd')
        rows = {m['id']: m for m in models['models']}
        for base in ('claude-haiku-5-5', 'claude-haiku-5-5-thinking'):
            self.assertEqual(rows[base]['supported_efforts'], ['low', 'medium', 'high', 'xhigh', 'max'])
        self.assertEqual(rows['composer-2.5']['supported_efforts'], [])
        self.assertEqual(rows['auto']['display'], 'Auto')
        self.assertTrue(all('(current)' not in m['display'] and '(default)' not in m['display'] for m in rows.values()))
        result = adapter.run(self.request(model='claude-haiku-5-5', effort='high'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['warnings'], [])
        self.assertEqual(result['model_requested'], 'claude-haiku-5-5-high')
        self.assertEqual(result['model_actual'], 'claude-haiku-5-5-high')
        self.assertIn('claude-haiku-5-5-high', [c['argv'] for c in self.log() if c['model_call']][-1])
        self.calls.unlink()
        for name, model in (('unknown', 'nonexistent'), ('wrong-effort', 'claude-haiku-5-5')):
            req = self.request(name, model=model, effort='unavailable')
            result = adapter.run(req)
            self.assertEqual((result['status'], result['warnings']), ('failed', ['model_not_found']))
            self.assertIsNone(result['exit_code'])
            self.assertEqual(self.log(), [])

    def test_auth_missing_exit_zero_and_no_pii_in_any_return_value(self):
        for scenario, state in (('success', 'ok'), ('auth-missing', 'missing')):
            adapter = self.adapter(scenario)
            auth = adapter.auth_status()
            self.assertEqual(auth, dict(state=state, method='unknown', plan='Premium', source='probe'))
            outputs = [auth, adapter.detect(), adapter.list_models(), adapter.quota(),
                       adapter.run(self.request(scenario))]
            encoded = json.dumps(outputs)
            for private in ('fake-user@example.invalid', 'fake-cursor-userId', 'fake-cursor-agentHost',
                            'fake-cursor-lastRequestId', 'fake-cursor-firstName'):
                self.assertNotIn(private, encoded)
        self.assertEqual(self.adapter().quota(), dict(windows=[], allowed=None, source='none'))

    def test_whole_document_bounds_types_and_probe_failures(self):
        adapter = self.adapter()
        good = '{"isAuthenticated": true}'
        for payload, expected in ((good, 'ok'), (good + '\n' + good, 'unknown'),
                (good + 'junk', 'unknown'), ('[]', 'unknown'), ('{"isAuthenticated": 1}', 'unknown'),
                ('{', 'unknown'), (None, 'unknown')):
            with mock.patch.object(adapter, '_probe', return_value=payload):
                self.assertEqual(adapter.auth_status()['state'], expected)
        runner = proc.run
        file = self.directory / 'probe.txt'
        def injected(argv, **kwargs):
            return runner([sys.executable, '-B', '-c',
                'import sys; sys.stdout.buffer.write(open(sys.argv[1],"rb").read())', str(file)], **kwargs)
        for text, expected in ((good + ' ' * (65536-len(good)), 'ok'),
                               (good + '\n' * 65536, 'unknown'), (good + ' ' * 65536, 'unknown')):
            file.write_text(text)
            with mock.patch.object(proc, 'run', side_effect=injected):
                self.assertEqual(adapter.auth_status()['state'], expected)
        for payload in ('', 'nonsense', 'Available models\n\nbad line\n', None):
            with mock.patch.object(adapter, '_probe', return_value=payload):
                self.assertEqual(adapter.list_models(), dict(models=None, source='none'))

    def test_environment_never_contains_api_keys(self):
        adapter = self.adapter(CURSOR_API_KEY='secret', OTHER_API_KEY='secret', OPENAI_API_KEY='secret')
        runner = proc.run
        def inspected(argv, **kwargs):
            if Path(argv[0]).name in ('agent', 'agent.cmd'):
                self.assertEqual(kwargs['env']['CURSOR_CONFIG_DIR'], self.env['CURSOR_CONFIG_DIR'])
                self.assertFalse(any(k.endswith('_API_KEY') for k in kwargs['env']))
            return runner(argv, **kwargs)
        with mock.patch.object(proc, 'run', side_effect=inspected):
            adapter.auth_status()
            self.assertEqual(adapter.run(self.request())['status'], 'completed')

    def test_add_dir_preflight_and_forbidden_argv(self):
        for kind in ('missing', 'file', 'relative'):
            raw = load_path(MANIFEST)
            target = self.directory / ('grant-' + kind)
            if kind == 'file':
                target.touch()
            raw['exec']['profiles']['review']['extra'] += ['--add-dir', 'relative' if kind == 'relative' else str(target)]
            result = self.adapter(manifest=Manifest(raw)).run(self.request(kind))
            self.assertEqual((result['status'], result['warnings']), ('blocked', ['add_dir_invalid']))
            self.assertEqual(self.log(), [])
        raw = load_path(MANIFEST)
        raw['exec']['profiles']['review']['extra'] += ['--mode=ask']
        with self.assertRaises(ManifestError):
            Manifest(raw)
        for tokens in (['--force'], ['-f'], ['--yolo'], ['--api-key', 'x'], ['--approve-mcps'],
                       ['--sandbox', 'disabled'], ['--sandbox=disabled'], ['--resume', 'x']):
            raw = load_path(MANIFEST)
            raw['exec']['profiles']['write']['extra'] += tokens
            with self.assertRaises(ManifestError):
                Manifest(raw)

    def test_unknown_display_missing_result_and_failure_markers(self):
        adapter = self.adapter()
        adapter.list_models()
        self.assertIsNone(adapter._extract([dict(type='system', subtype='init', model='Unknown display')])['model_actual'])
        exited = proc.ProcResult('exited', 1, .01, 0, 0, 0, '', False, None)
        for text, status, warnings in (
                ('Cannot use this model: fake', 'failed', ['model_not_found']),
                ('Login required', 'auth', []), ('Workspace is untrusted', 'blocked', []),
                ('Usage limit reached', 'failed', ['quota_suspected']),
                ('Shell command rejected: --force is absent', 'failed', ['shell_blocked']),
                ('', 'failed', ['final_event_missing'])):
            self.assertEqual(adapter._classify({'events': [{'_raw': text}], 'counts': {}}, exited), (status, warnings))
