"""Closing-review regressions, using only temp repositories and fake CLIs."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest import mock

from tests.unit.test_do import DoFixture, FAST, FAKE
from tests.unit._timing import scaled
from tests.unit.test_run_result import valid_result
from orcexe import doctor, gate_lite as gl, route, run
from orcexe.adapters.base import safe_write_text, safe_write_json, safe_append_jsonl


class ClosingReviewTests(DoFixture):
    def wrapper(self, tool, action='', injection=None):
        """Adapt the L2 wrapper ideas; no external probe path is needed by tests."""
        fake = FAKE.with_name({'codex': 'codex', 'claude-code': 'claude', 'cursor': 'agent'}[tool])
        path = self.root / ('wrapped-' + tool)
        path.write_text('#!' + sys.executable + '\n' +
            'import json, os, subprocess, sys\nfrom pathlib import Path\n' +
            'executing = "exec" in sys.argv or "-p" in sys.argv\n' +
            'if not executing: os.execv(' + repr(sys.executable) + ', ' + repr([sys.executable, '-B', str(fake)]) + ' + sys.argv[1:])\n' +
            'if executing:\n' + ''.join('    ' + line + '\n' for line in (action or 'pass').splitlines()) +
            'p = subprocess.run(' + repr([sys.executable, '-B', str(fake)]) + ' + sys.argv[1:], stdout=subprocess.PIPE)\n' +
            'for line in p.stdout.decode().splitlines():\n' +
            ('    if executing and json.loads(line).get("type") == "result":\n'
             '        print(' + repr(json.dumps(injection)) + ')\n' if injection else '') +
            '    print(line)\nsys.exit(p.returncode)\n')
        path.chmod(0o700)
        self.env['ORCEXE_BIN_' + tool.upper().replace('-', '_')] = str(path)
        return path

    def test_b1_codex_plant_never_runs_and_reports_main_changed(self):
        marker = self.root / 'planted-ran'
        config = 'version: 1\ncommands:\n  - id: planted\n    argv: ' + json.dumps(
            [sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)]) + '\n'
        self.wrapper('codex', 'Path(%r).write_text(%r)' % (str(self.repo / '.orcexe/commands.yaml'), config))
        result = self.invoke()
        self.assertEqual(result['failed_check'], 'main_repository_changed', result)
        self.assertNotEqual(result['verdict'], self.accepted_verdict)
        self.assertFalse(marker.exists())

    def test_b1_existing_config_byte_changes_are_detected(self):
        config = self.repo / '.orcexe/risk-map.yaml'
        config.parent.mkdir()
        config.write_text('version: 1\nrisky_words: []\n')
        with self.executor(before=lambda wt: config.write_text('version: 1\nrisky_words: [payments]\n')):
            result = self.invoke()
        self.assertEqual(result['failed_check'], 'main_repository_changed', result)

    def test_b1_gate_receives_frozen_commands_even_if_file_appears_later(self):
        gate = self.gate
        marker = self.root / 'late-planted-ran'
        def late(**kwargs):
            self.assertIsInstance(kwargs['commands'], list)
            (self.repo / '.orcexe/commands.yaml').write_text('version: 1\ncommands:\n  - id: planted\n    argv: ' + json.dumps(
                [sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)]) + '\n')
            return gate(**kwargs)
        self.gate = late
        self.assertEqual(self.invoke()['verdict'], self.accepted_verdict)
        self.assertFalse(marker.exists())

    def test_b2_claude_inject_quoted_auth_is_accepted(self):
        self.claude_scenario = 'success'
        self.wrapper('claude-code', injection={'type': 'user', 'message': {'content': [
            {'type': 'tool_result', 'content': 'Invalid API key; HTTP 401; please run /login; model_not_found'}]}})
        self.assertEqual(self.invoke(model_constraint={'tools': ['claude-code']})['verdict'], self.accepted_verdict)

    def test_b2_agent_inject_quoted_markers_is_accepted(self):
        self.cursor_scenario = 'success'
        self.wrapper('cursor', injection={'type': 'tool_call', 'content':
            'untrusted; Login required; Cannot use this model; quota exceeded'})
        self.assertEqual(self.invoke(model_constraint={'tools': ['cursor']})['verdict'], self.accepted_verdict)

    def test_b2_only_structured_errors_and_non_json_diagnostics_classify(self):
        from dataclasses import replace
        from orcexe import proc
        ended = proc.ProcResult('exited', 0, .01, 0, 0, 0, '', False, None)
        quote = 'Invalid API key; authentication_failed; Login required; untrusted; quota exceeded'
        success = dict(type='result', subtype='success', is_error=False, terminal_reason='completed', result=quote)
        for adapter in (self.claude_adapter(), self.cursor_adapter()):
            self.assertEqual(adapter._classify({'events': [success]}, ended), ('completed', []))
            failed = replace(ended, returncode=1)
            # An actual JSON event cannot impersonate a parser-generated raw line.
            local = adapter._run_context if adapter.manifest.tool == 'claude-code' else adapter._context
            local.value = {'markers': set()}
            try:
                adapter._observe({'_raw': quote})
                self.assertEqual(adapter._classify({'events': [{'_raw': quote}]}, failed)[0], 'failed')
            finally:
                del local.value
            self.assertEqual(adapter._classify({'events': []}, replace(failed, stderr_tail=json.dumps(
                {'type': 'assistant', 'content': quote})))[0], 'failed')
        claude = self.claude_adapter()
        error = dict(success, is_error=True, result='', terminal_reason='authentication_error')
        self.assertEqual(claude._classify({'events': [error]}, ended), ('auth', []))
        cursor = self.cursor_adapter()
        error = dict(success, subtype='error', is_error=True, result='Usage limit reached')
        self.assertEqual(cursor._classify({'events': [error]}, replace(ended, returncode=1)),
                         ('failed', ['quota_suspected']))

    def test_b3_aged_cache_routes_claude_and_doctor_shows_age(self):
        self.claude_scenario = 'success'
        adapter = self.claude_adapter()
        path = adapter._cache_path()
        path.parent.mkdir(parents=True)
        old = time.time() - 30 * 86400
        path.write_text(json.dumps({'observed_at': old, 'rate_limit_info': {'status': 'rejected',
            'resetsAt': old, 'unifiedWindows': {'five_hour': {'utilization': 1, 'resetsAt': old}}}}))
        quota = adapter.quota()
        self.assertEqual((quota['source'], quota['allowed']), ('none', None))
        self.assertGreater(quota['age_s'], 6 * 3600)
        result = self.invoke(dry_run=True, model_constraint={'tools': ['claude-code']})
        self.assertEqual(result['verdict'], 'planned', result)
        adapter._doctor_budgets = doctor._budgets(1)
        _, checks = doctor._tool_report(adapter, False, None)
        self.assertTrue(any('cache age' in c['message'] for c in checks))
        self.assertFalse(any(c['id'].endswith('quota.exhausted') for c in checks))

    def test_b3_reset_window_and_unknown_quota_do_not_exhaust_route(self):
        adapter = self.claude_adapter()
        path = adapter._cache_path()
        path.parent.mkdir(parents=True)
        now = time.time()
        path.write_text(json.dumps({'observed_at': now - 10, 'rate_limit_info': {'status': 'rejected',
            'resetsAt': now - 1, 'unifiedWindows': {'five_hour': {'utilization': 1, 'resetsAt': now - 1},
            'seven_day': {'utilization': .5, 'resetsAt': now + 100}}}}))
        quota = adapter.quota()
        self.assertIsNone(quota['allowed'])
        self.assertEqual([w['name'] for w in quota['windows']], ['seven_day'])
        self.assertEqual(route._quota(dict(source='stream', allowed=None, windows=[{'used_pct': 100}])), (None, None))

    def test_b4_agent_symlink_probe_and_parent_attack_leave_victim_unchanged(self):
        self.cursor_scenario = 'success'
        victim = self.root / 'victim.txt'
        victim.write_bytes(b'UNCHANGED\n')
        # The original probe attacks the --add-dir grant. Also model an escape
        # to its parent to exercise O_NOFOLLOW rather than only grant isolation.
        action = ('brief = Path(sys.argv[sys.argv.index("--add-dir") + 1])\n'
                  'assert brief.name == "brief"\n'
                  'assert [p.name for p in brief.iterdir()] == ["brief.md"]\n'
                  'assert brief.stat().st_mode & 0o777 == 0o700\n'
                  'assert (brief / "brief.md").stat().st_mode & 0o777 == 0o600\n'
                  'events = brief.parent / "events.jsonl"\n'
                  'events.unlink()\n'
                  'events.symlink_to(%r)' % str(victim))
        self.wrapper('cursor', action)
        result = self.invoke(model_constraint={'tools': ['cursor']})
        self.assertNotEqual(result['verdict'], self.accepted_verdict)
        self.assertEqual(victim.read_bytes(), b'UNCHANGED\n')


    def test_p2_gate_uses_state_dir_and_briefs_are_private(self):
        self.env['ORCEXE_KEEP_GATE_DIR'] = '1'
        result = self.invoke()
        self.assertEqual(result['verdict'], self.accepted_verdict, result)
        self.assertEqual(len(list((self.home / 'state/gate').iterdir())), 1)
        self.assertFalse((self.home / '.cache/orcexe/gate').exists())
        directory = self.repo / '.orcexe/runs' / result['run_id']
        for name in ('brief-src.md', 'brief.md'):
            self.assertEqual((directory / name).stat().st_mode & 0o777, 0o600)

    def test_b4_all_run_writers_refuse_file_and_directory_symlinks(self):
        victim = self.root / 'victim'
        victim.write_text('unchanged')
        output = self.root / 'output'
        output.symlink_to(victim)
        for write in (lambda: safe_write_text(output, 'bad'), lambda: safe_write_json(output, {}),
                      lambda: safe_append_jsonl(output, {})):
            with self.assertRaises(OSError):
                write()
            self.assertEqual(victim.read_text(), 'unchanged')
        folder = self.root / 'gate'
        folder.symlink_to(self.home, target_is_directory=True)
        with self.assertRaises(OSError):
            safe_write_text(folder / 'bad.log', 'bad')
        self.assertFalse((self.home / 'bad.log').exists())

    def test_b5_zero_unittest_and_pytest_evidence_is_not_seen(self):
        directory = self.root / 'logs'
        cases = [('unittest', 'Ran 0 tests in 0.00s\nOK', 0),
                 ('unittest', 'Ran 0 tests in 0.00s\nNO TESTS RAN', 5),
                 ('pytest', 'no tests ran in 0.00s', 5)]
        for name, text, code in cases:
            command = gl.Command(name, [sys.executable, '-c', 'print(%r); raise SystemExit(%d)' % (text, code), name])
            status, detail = gl._project([command], self.repo, directory, scaled(5), time.monotonic() + scaled(10))
            self.assertEqual(status, 'not_seen')
            self.assertIn('zero_tests', detail)

    def test_p2_manual_run_pipes_held_is_exit_two_and_visible(self):
        result = valid_result(self.root)
        result['warnings'] = ['pipes_held']
        self.assertEqual(run.result_exit_code(result), 2)
        self.assertIn('pipes_held', run._human(result))
