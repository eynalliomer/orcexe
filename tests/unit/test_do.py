"""Fast-lane orchestration using only isolated repositories and fake Codex."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import time
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit._timing import scaled

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'plugins/orcexe/src'))
from orcexe import classify, cli, do, exitcodes, gate_lite, ledger, proc, route, worktree
from orcexe.adapters.codex import CodexAdapter
from orcexe.adapters.base import RunRequest

FAKE = ROOT / 'tests/fixtures/fake-cli/codex'
FAST = 'Add a docstring to src/app.py'
EVENTS = ['task.classified', 'task.dispatched', 'orchestrator.usage',
          'attempt.ended', 'gate.result', 'task.state']
EXTEND = ' " Added documentation."\n'


class DoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='orcexe-do-', dir=str(ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        self.home = self.root / 'home'
        self.home.mkdir()
        self.calls = self.root / 'calls.jsonl'
        self.env = dict(PATH=os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
            HOME=str(self.home), CODEX_HOME=str(self.home / 'codex'),
            XDG_CACHE_HOME=str(self.home / 'cache'),
            ORCEXE_BIN_CODEX=str(FAKE), ORCEXE_FAKE_CALLS=str(self.calls),
            ORCEXE_FAKE_SCENARIO='success', ORCEXE_FAKE_EDIT_FILE='src/app.py',
            ORCEXE_FAKE_EDIT_TEXT=EXTEND, PYTHONDONTWRITEBYTECODE='1',
            GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
            GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')
        for name in ('SystemRoot', 'SYSTEMROOT', 'WINDIR'):
            if name in os.environ:
                self.env[name] = os.environ[name]
        self.git('init', '-q')
        self.git('config', 'user.name', 'Test')
        self.git('config', 'user.email', 'test@example.invalid')
        (self.repo / 'README.md').write_text('Example\n')
        (self.repo / 'src').mkdir()
        (self.repo / 'src/app.py').write_text('def app():\n    """Return nothing."""')
        (self.repo / 'tests').mkdir()
        (self.repo / 'tests/__init__.py').touch()
        (self.repo / 'tests/test_app.py').write_text(
            'import unittest\nfrom src.app import app\n'
            'class AppTest(unittest.TestCase):\n'
            '    def test_app(self):\n        self.assertIsNone(app())\n')
        self.git('add', '.')
        self.git('commit', '-qm', 'initial')
        self.original = (self.repo / 'src/app.py').read_bytes()
        self.factory = do.CodexAdapter
        self.gate = gate_lite.gate
        self.addCleanup(mock.patch.stopall)
        mock.patch('builtins.input', side_effect=AssertionError('stdin read')).start()

    def git(self, *args, cwd=None):
        result = subprocess.run(['git', *args], cwd=str(cwd or self.repo), env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=scaled(10))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def adapter(self):
        return self.factory(env=self.env, allow_extra=tuple(k for k in self.env if k.startswith('ORCEXE_FAKE_')) + ('ORCEXE_FAKE_APPSERVER_SCENARIO',),
            detect_timeout_s=scaled(3), appserver_timeout_s=scaled(3), auth_timeout_s=scaled(3),
            kill_grace_s=scaled(.15))

    def invoke(self, task=FAST, **options):
        # Keep production clocks real; scale every test-supplied deadline.
        def gate(**kwargs):
            return self.gate(command_timeout_s=scaled(10), total_timeout_s=scaled(20), **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(do, 'CodexAdapter', side_effect=self.adapter), \
                mock.patch.object(do.gate_lite, 'gate', side_effect=gate):
            return do.run_do(task, self.repo, timeout=scaled(options.pop('timeout', 10)), **options)

    def log(self):
        return ledger.Ledger(ledger.default_path(self.repo))

    def call_log(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_no_executor(self):
        self.assertFalse(any(call['argv'][0] == 'exec' for call in self.call_log()))
        self.assertEqual(worktree.list_managed(self.repo), [])

    def check(self, result, verdict, code, failed=None):
        self.assertEqual((result['verdict'], result['exit_code']), (verdict, code), result)
        records = list(self.log().records())
        expected = EVENTS + (['lane.escalated'] if verdict == 'escalated' else [])
        self.assertEqual([r['event'] for r in records], expected)
        self.assertTrue(self.log().verify()['ok'])
        directory = self.repo / '.orcexe/runs' / result['run_id']
        status = (directory / 'status.md').read_text()
        self.assertIn('Verdict: ' + verdict, status)
        self.assertIn('agent says:', status)
        receipt = json.loads((directory / 'gate.json').read_text())
        self.assertEqual(result['changed_files'], [f['path'] for f in receipt['diff']['files']])
        self.assertEqual(result['failed_check'], failed)
        self.assertEqual(records[2]['data'], {'input': None, 'output': None, 'source': 'not_seen'})
        self.assertEqual(records[-2 if verdict == 'escalated' else -1]['data']['to'], verdict)
        executions = [c for c in self.call_log() if c['argv'][0] == 'exec']
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]['cwd'], result['worktree'])
        self.assertIn(result['worktree'], executions[0]['argv'])
        self.assertEqual(self.git('status', '--porcelain'), '')
        self.assertEqual((self.repo / 'src/app.py').read_bytes(), self.original)
        self.assertFalse((self.repo / '.orcexe/do.lock').exists())
        self.assertTrue(Path(result['worktree']).exists())
        if verdict == 'accepted':
            self.assertTrue('merge --ff-only' in status or 'cherry-pick' in status)
            self.assertEqual(result['exit_code'], 0)
        else:
            self.assertIn('Nothing was landed', status)
            self.assertNotIn('land_commands', result)
            self.assertNotIn('merge --ff-only', status)
        return directory

    def test_c11_english(self):
        self.check(self.invoke(), 'accepted', exitcodes.OK)

    def test_c11_turkish(self):
        self.check(self.invoke("src/app.py'ye docstring ekle"), 'accepted', exitcodes.OK)

    def test_runtime_quota(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'quota'
        gather = route.gather_access
        def exhaust_after_access(adapters):
            access = gather(adapters)
            self.env['ORCEXE_FAKE_APPSERVER_SCENARIO'] = 'quota-exhausted'
            adapters[0]._env['ORCEXE_FAKE_APPSERVER_SCENARIO'] = 'quota-exhausted'
            return access
        with mock.patch.object(do.route, 'gather_access', side_effect=exhaust_after_access):
            self.check(self.invoke(), 'failed', exitcodes.QUOTA, 'run_status')

    def test_auth_401(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'auth-401'
        self.check(self.invoke(), 'failed', exitcodes.AUTH, 'run_status')

    def test_invalid_model(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'invalid-model'
        self.check(self.invoke(), 'failed', exitcodes.FAIL, 'run_status')

    def test_stall_timeout(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'stall'
        self.check(self.invoke(timeout=.4), 'failed', exitcodes.TIMEOUT, 'run_status')

    def test_stalled_maps_to_timeout(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'stall'
        def request(**kwargs):
            return RunRequest(stall_s=scaled(.4), **kwargs)
        with mock.patch.object(do, 'RunRequest', side_effect=request):
            result = self.invoke()
        self.check(result, 'failed', exitcodes.TIMEOUT, 'run_status')
        self.assertEqual(result['run_status'], 'stalled')

    def test_success_without_hook_is_empty_diff(self):
        # Existing success fixture writes nothing; keep that default unchanged.
        self.env.pop('ORCEXE_FAKE_EDIT_FILE')
        self.env.pop('ORCEXE_FAKE_EDIT_TEXT')
        self.check(self.invoke(), 'failed', exitcodes.FAIL, 'run_status')

    def test_empty_hook(self):
        self.env['ORCEXE_FAKE_EDIT_TEXT'] = ''
        self.check(self.invoke(), 'failed', exitcodes.FAIL, 'run_status')

    def test_default_write_creates_out_of_scope_file(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'write'
        self.env.pop('ORCEXE_FAKE_EDIT_FILE')
        self.check(self.invoke(), 'escalated', exitcodes.FAIL, 'scope')

    def test_out_of_scope(self):
        self.env['ORCEXE_FAKE_EDIT_FILE'] = 'README.md'
        self.check(self.invoke(), 'escalated', exitcodes.FAIL, 'scope')

    def test_code_changed(self):
        self.env['ORCEXE_FAKE_EDIT_TEXT'] = '\n    return 1\n'
        self.check(self.invoke(), 'escalated', exitcodes.FAIL, 'code_unchanged')

    def test_nonfast_lanes(self):
        for task, lane in [('tüm testleri düzelt', 'standard'), ('auth modülünü düzelt', 'risky')]:
            with self.subTest(lane=lane):
                result = self.invoke(task)
                self.assertEqual(result['exit_code'], exitcodes.UNSUPPORTED)
                self.assertEqual(result['lane'], lane)
                self.assertNotIn(task, result['message'])
                self.assertIn('orcexe plan', result['message'])
                self.assert_no_executor()
                directory = self.repo / '.orcexe/runs' / result['run_id']
                self.assertIn('Verdict: blocked', (directory / 'status.md').read_text())
        records = list(self.log().records())
        self.assertEqual([r['event'] for r in records], ['task.classified', 'task.state'] * 2)
        self.assertEqual([r['data']['reason'] for r in records if r['event'] == 'task.state'],
                         ['lane_standard', 'lane_risky'])
        self.assertTrue(self.log().verify()['ok'])

    def test_route_failure_auth(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'auth-missing'
        result = self.invoke()
        self.assertEqual(result['exit_code'], exitcodes.AUTH, result)
        self.assert_no_executor()
        self.assertIn('auth_missing', result['message'])
        directory = self.repo / '.orcexe/runs' / result['run_id']
        self.assertIn('Nothing was landed', (directory / 'status.md').read_text())
        self.assertEqual([r['event'] for r in self.log().records()], ['task.classified', 'task.state'])
        self.assertTrue(self.log().verify()['ok'])

    def test_route_failure_mixed_quota_skips(self):
        self.env['ORCEXE_FAKE_APPSERVER_SCENARIO'] = 'quota-exhausted'
        result = self.invoke()
        self.assertEqual(result['exit_code'], exitcodes.FAIL, result)
        # Mixed skips: the shipped fallback model is absent from the live list.
        self.assertEqual({s['reason'] for s in result['route']['skipped']},
                         {'quota_exhausted', 'model_unavailable'})
        self.assert_no_executor()
        self.assertTrue(self.log().verify()['ok'])

    def test_route_failure_all_quota_skips(self):
        self.env['ORCEXE_FAKE_APPSERVER_SCENARIO'] = 'quota-exhausted'
        result = self.invoke(model_constraint={'force': {
            'tool': 'codex', 'model': 'gpt-6.1-sol', 'effort': 'medium'}})
        self.assertEqual(result['exit_code'], exitcodes.QUOTA, result)
        self.assert_no_executor()
        self.assertEqual([r['event'] for r in self.log().records()], ['task.classified', 'task.state'])
        self.assertTrue(self.log().verify()['ok'])

    def test_not_a_repo(self):
        self.git('init', '--bare', '-q', str(self.home))
        with mock.patch.dict(os.environ, self.env, clear=True):
            result = do.run_do(FAST, self.home)
        self.assertEqual(result['exit_code'], exitcodes.UNSUPPORTED)
        self.assertEqual(result['mode'], 'everyday')
        self.assertIn('everyday tasks arrive in v0.4', result['message'])
        self.assertFalse((self.home / '.orcexe').exists())
        self.assert_no_executor()

    def test_lock_held(self):
        lock = self.repo / '.orcexe/do.lock'
        lock.parent.mkdir()
        lock.write_text(json.dumps({'pid': os.getpid(), 'utc': datetime.now(timezone.utc).isoformat()}))
        result = self.invoke()
        self.assertEqual(result['exit_code'], exitcodes.FAIL)
        self.assertIn('another orcexe do is running', result['message'])
        self.assertTrue(lock.exists())
        self.assert_no_executor()
        self.assertEqual(list(self.log().records()), [])

    def test_stale_lock_replaced_only_if_dead(self):
        lock = self.repo / '.orcexe/do.lock'
        lock.parent.mkdir()
        old = datetime.now(timezone.utc) - timedelta(hours=7)
        lock.write_text(json.dumps({'pid': 2147483647, 'utc': old.isoformat()}))
        os.utime(lock, (old.timestamp(), old.timestamp()))
        self.check(self.invoke(), 'accepted', exitcodes.OK)

    def test_dry_run_writes_nothing(self):
        result = self.invoke(dry_run=True)
        self.assertEqual(result['exit_code'], exitcodes.OK, result)
        self.assertEqual(result['verdict'], 'planned')
        self.assertEqual(result['route']['tool'], 'codex')
        self.assertIn('route:', result['message'])
        self.assert_no_executor()
        self.assertFalse((self.repo / '.orcexe').exists())

    def test_json_cli(self):
        result = self.invoke()
        output = io.StringIO()
        with mock.patch('orcexe.do.run_do', return_value=result), redirect_stdout(output):
            code = cli.main(['do', FAST, '--repo', str(self.repo), '--json'])
        result = json.loads(output.getvalue())
        self.assertEqual(code, exitcodes.OK)
        self.assertEqual(result['task_preview'], FAST)
        self.assertEqual(output.getvalue().count(FAST), 1)
        self.check(result, 'accepted', exitcodes.OK)

    def test_secret_task_and_private_briefs(self):
        key = 'sk-proj-' + secrets.token_hex(24)
        # A tracked filename lets the existing grammar mask a resolved target,
        # keeping a secret-bearing task fast without changing classification.
        target = 'src/' + key + '.py'
        (self.repo / target).write_bytes(self.original)
        self.git('add', target)
        self.git('commit', '-qm', 'extra doc target')
        self.env['ORCEXE_FAKE_EDIT_FILE'] = target
        result = self.invoke('Add a docstring to ' + target)
        directory = self.check(result, 'accepted', exitcodes.OK)
        for path in [directory / 'status.md', directory / 'gate.json', self.log().path]:
            self.assertNotIn(key.encode(), path.read_bytes(), str(path))
        self.assertNotIn(key, json.dumps(result))
        for name in ('brief-src.md', 'brief.md'):
            self.assertIn(key, (directory / name).read_text())
        self.assertEqual((directory.parent.stat().st_mode & 0o777), 0o700)
        self.assertIn('/.orcexe/', (self.repo / '.git/info/exclude').read_text())

    def test_snapshot_precedes_executor(self):
        snapshot = gate_lite.snapshot_controls
        ready = []
        def snap(*args):
            ready.append(True)
            self.assertFalse(any(c['argv'][0] == 'exec' for c in self.call_log()))
            return snapshot(*args)
        original_run = CodexAdapter.run
        def run(adapter, req):
            self.assertTrue(ready)
            return original_run(adapter, req)
        # The gate snapshots again; only constrain the first observation.
        def first_snap(*args):
            return snapshot(*args) if ready else snap(*args)
        with mock.patch.object(do.gate_lite, 'snapshot_controls', side_effect=first_snap), \
                mock.patch.object(CodexAdapter, 'run', run):
            self.check(self.invoke(), 'accepted', exitcodes.OK)

    def test_internal_error_is_redacted_and_unlocks(self):
        key = 'sk-proj-' + secrets.token_hex(24)
        with mock.patch.object(CodexAdapter, 'run', side_effect=RuntimeError(key)):
            result = self.invoke()
        self.assertEqual(result['exit_code'], exitcodes.INTERNAL)
        self.assertNotIn(key, json.dumps(result))
        directory = self.repo / '.orcexe/runs' / result['run_id']
        self.assertNotIn(key, (directory / 'internal-error.txt').read_text())
        self.assertNotIn('Traceback', result['message'])
        self.assertIn('Nothing was landed', (directory / 'status.md').read_text())
        self.assertFalse((self.repo / '.orcexe/do.lock').exists())
        records = list(self.log().records())
        self.assertEqual([r['event'] for r in records], EVENTS[:3] + ['task.state'])
        self.assertEqual(records[-1]['data']['reason'], 'internal_error')
        self.assertTrue(self.log().verify()['ok'])

    def test_head_moved_prints_cherry_pick(self):
        real_gate = self.gate
        def moving_gate(**kwargs):
            receipt = real_gate(**kwargs)
            self.git('commit', '--allow-empty', '-qm', 'main advanced')
            return receipt
        self.gate = moving_gate
        result = self.invoke()
        self.check(result, 'accepted', exitcodes.OK)
        self.assertIn('cherry-pick', result['land_commands'][0])
        self.assertNotIn('merge --ff-only', result['message'])

    def test_lock_release_error_cannot_report_success(self):
        unlink = Path.unlink
        def cannot_release(path, *args, **kwargs):
            if path == self.repo / '.orcexe/do.lock':
                raise OSError('lock release unavailable')
            return unlink(path, *args, **kwargs)
        with mock.patch.object(Path, 'unlink', cannot_release):
            result = self.invoke()
        self.assertEqual((result['verdict'], result['exit_code']), ('failed', exitcodes.INTERNAL))
        self.assertNotIn('land_commands', result)
        self.assertIn('Nothing was landed', result['message'])
        records = list(self.log().records())
        self.assertEqual([r['event'] for r in records], EVENTS + ['task.state'])
        self.assertEqual(records[-1]['data']['reason'], 'internal_error')
        self.assertTrue(self.log().verify()['ok'])

    def executor(self, before=None, after=None):
        """Inject misbehaviour around the real fake's exec call, as L2 did."""
        real = proc.run
        def run(argv, **kwargs):
            is_exec = len(argv) > 1 and argv[1] == 'exec'
            if is_exec and before:
                before(Path(kwargs['cwd']))
            result = real(argv, **kwargs)
            if is_exec and after:
                after(Path(kwargs['cwd']))
            return result
        return mock.patch.object(proc, 'run', side_effect=run)

    def assert_main_changed(self, evil):
        with self.executor(after=evil):
            result = self.invoke()
        self.assertEqual((result['verdict'], result['exit_code'], result['failed_check']),
                         ('failed', exitcodes.FAIL, 'main_repository_changed'), result)
        self.assertNotIn('land_commands', result)
        directory = self.repo / '.orcexe/runs' / result['run_id']
        self.assertFalse((directory / 'gate.json').exists())
        self.assertIn('main_repository_changed', (directory / 'status.md').read_text())
        records = list(self.log().records())
        self.assertNotIn('gate.result', [r['event'] for r in records])
        self.assertEqual(records[-1]['data']['reason'], 'main_repository_changed')

    def test_l2_a1_executor_moves_main_branch_ref(self):
        def evil(wt):
            tree = self.git('rev-parse', 'HEAD^{tree}', cwd=wt)
            commit = self.git('commit-tree', tree, '-p', 'HEAD', '-m', 'evil', cwd=wt)
            branch = self.git('symbolic-ref', 'HEAD')
            self.git('update-ref', branch, commit, cwd=wt)
        self.assert_main_changed(evil)

    def test_l2_a1b_executor_commits_evil_to_main(self):
        def evil(wt):
            self.git('commit', '--allow-empty', '-qm', 'ungated executor commit')
            (self.repo / 'evil.py').write_text('EVIL\n')
            self.git('add', 'evil.py')
            self.git('commit', '-qm', 'ungated evil file')
        self.assert_main_changed(evil)

    def test_l2_a2_executor_writes_main_worktree(self):
        self.assert_main_changed(lambda wt: (self.repo / 'README.md').write_text('EVIL\n'))

    def test_l2_controls_checked_before_gate(self):
        def evil(wt):
            self.git('config', 'core.fsmonitor', 'evil-command')
        self.assert_main_changed(evil)

    def test_l2_other_refs_and_symbolic_head_are_observed(self):
        self.assert_main_changed(lambda wt: self.git('update-ref', 'refs/tags/evil', 'HEAD', cwd=wt))
        self.git('update-ref', '-d', 'refs/tags/evil')
        self.assert_main_changed(lambda wt: self.git('checkout', '-q', '-b', 'evil'))

    def land(self, result, expected):
        ref = 'refs/orcexe/accepted/' + result['task_id']
        self.assertIn(ref, result['land_commands'][0])
        self.assertNotIn('..', result['land_commands'][0])
        base = self.git('rev-parse', ref + '^')
        self.assertEqual(self.git('diff', '--name-only', base, ref), 'src/app.py')
        self.assertEqual(self.git('show', ref + ':src/app.py'), expected.decode().strip())
        command = subprocess.run(result['land_commands'][0], shell=True, env=self.env,
                                 capture_output=True, text=True, timeout=scaled(10))
        self.assertEqual(command.returncode, 0, command.stderr)
        self.assertEqual((self.repo / 'src/app.py').read_bytes(), expected)
        self.assertEqual(self.git('status', '--porcelain'), '')

    def test_l2_a4b_uncommitted_merge_lands_verified_bytes(self):
        result = self.invoke()
        self.assertEqual(result['verdict'], 'accepted', result)
        self.assertIn('merge --ff-only', result['land_commands'][0])
        self.land(result, self.original + EXTEND.encode())

    def test_l2_a4c_main_advanced_cherry_pick_lands_verified_bytes(self):
        real_gate = self.gate
        def moving(**kwargs):
            receipt = real_gate(**kwargs)
            self.git('commit', '--allow-empty', '-qm', 'main advanced')
            return receipt
        self.gate = moving
        result = self.invoke()
        self.assertEqual(result['verdict'], 'accepted', result)
        self.assertIn('cherry-pick', result['land_commands'][0])
        self.land(result, self.original + EXTEND.encode())

    def test_l2_a6_leftover_child_edits_after_gate(self):
        # A detached child waits for the gate, then repeatedly rewrites the
        # executor checkout. Handshakes keep the race deterministic under load.
        ready, stop = self.root / 'late-ready', self.root / 'late-stop'
        script = ('import pathlib,sys,time\n'
                  'rd,t,ready,stop=map(pathlib.Path,sys.argv[1:])\n'
                  'end=time.monotonic()+60\n'
                  'while not (rd/"gate.json").exists() and time.monotonic()<end: time.sleep(.01)\n'
                  'while not stop.exists() and time.monotonic()<end:\n'
                  ' t.write_text("EVIL = 1\\n"); ready.touch(); time.sleep(.01)\n')
        children = []
        def spawn(wt):
            rd = next((self.repo / '.orcexe/runs').iterdir())
            children.append(subprocess.Popen([sys.executable, '-c', script, str(rd), str(wt / 'src/app.py'),
                str(ready), str(stop)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True))
        real_gate = self.gate
        def wait_for_late_writer(**kwargs):
            receipt = real_gate(**kwargs)
            end = time.monotonic() + scaled(10)
            while not ready.exists() and time.monotonic() < end:
                time.sleep(.01)
            self.assertTrue(ready.exists())
            return receipt
        self.gate = wait_for_late_writer
        try:
            with self.executor(before=spawn):
                result = self.invoke()
            self.assertEqual(result['verdict'], 'accepted', result)
            self.land(result, self.original + EXTEND.encode())
            self.assertIsNone(children[0].poll())
        finally:
            stop.touch()
            for child in children:
                child.wait(timeout=scaled(10))

    def test_l2_a11_pipes_held_is_unverified(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'background-child'
        def legit(wt):
            with (wt / 'src/app.py').open('a') as handle:
                handle.write(EXTEND)
        with self.executor(after=legit):
            result = self.invoke()
        self.assertEqual((result['verdict'], result['exit_code']), ('unverified', exitcodes.FAIL), result)
        self.assertIn('pipes_held', result['message'])
        self.assertNotIn('land_commands', result)
        self.assertEqual(list(self.log().records())[-1]['data']['reason'], 'pipes_held')

    def test_c12_global_identity_is_frozen(self):
        self.git('config', '--unset', 'user.name')
        self.git('config', '--unset', 'user.email')
        for key in tuple(self.env):
            if key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_')):
                del self.env[key]
        config = self.home / '.gitconfig'
        config.write_text('[user]\n\tname = Global User\n\temail = global@example.invalid\n')
        for explicit in (False, True):
            with self.subTest(explicit_global_path=explicit):
                if explicit:
                    alternate = self.home / 'alternate.gitconfig'
                    config.rename(alternate)
                    self.env['GIT_CONFIG_GLOBAL'] = str(alternate)
                else:
                    del self.env['GIT_CONFIG_GLOBAL']
                result = self.invoke()
                self.assertEqual((result['verdict'], result['exit_code']), ('accepted', exitcodes.OK), result)
                ref = 'refs/orcexe/accepted/' + result['task_id']
                self.assertEqual(self.git('show', '-s', '--format=%an|%ae|%cn|%ce', ref),
                                 'Global User|global@example.invalid|Global User|global@example.invalid')

    def test_c12_environment_identity_is_frozen(self):
        self.git('config', '--unset', 'user.name')
        self.git('config', '--unset', 'user.email')
        self.env.update(GIT_AUTHOR_NAME='Environment Author', GIT_AUTHOR_EMAIL='author@env.invalid',
                        GIT_COMMITTER_NAME='Environment Committer', GIT_COMMITTER_EMAIL='committer@env.invalid')
        result = self.invoke()
        self.assertEqual((result['verdict'], result['exit_code']), ('accepted', exitcodes.OK), result)
        ref = 'refs/orcexe/accepted/' + result['task_id']
        self.assertEqual(self.git('show', '-s', '--format=%an|%ae|%cn|%ce', ref),
                         'Environment Author|author@env.invalid|Environment Committer|committer@env.invalid')

    def test_c12_environment_overrides_config_per_field(self):
        for role in ('author', 'committer'):
            self.git('config', role + '.name', 'Configured ' + role)
            self.git('config', role + '.email', role + '@config.invalid')
        del self.env['GIT_AUTHOR_EMAIL']
        del self.env['GIT_COMMITTER_NAME']
        self.env.update(GIT_AUTHOR_NAME='Environment Author', GIT_COMMITTER_EMAIL='committer@env.invalid')
        result = self.invoke()
        self.assertEqual(result['verdict'], 'accepted', result)
        ref = 'refs/orcexe/accepted/' + result['task_id']
        self.assertEqual(self.git('show', '-s', '--format=%an|%ae|%cn|%ce', ref),
                         'Environment Author|author@config.invalid|Configured committer|committer@env.invalid')

    def test_c12_missing_identity_blocks_after_routing_before_worktree(self):
        for key in tuple(self.env):
            if key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_')):
                del self.env[key]
        self.git('config', '--unset', 'user.name')
        self.git('config', '--unset', 'user.email')
        for present in (None, 'name', 'email'):
            with self.subTest(only_configured_field=present):
                if present:
                    self.git('config', 'user.' + present, 'Test' if present == 'name' else 'test@example.invalid')
                order = mock.Mock()
                with mock.patch.object(do.route, 'gather_access', wraps=route.gather_access) as access, \
                        mock.patch.object(do.route, 'route', wraps=route.route) as routing, \
                        mock.patch.object(do, '_resolve_identity', wraps=do._resolve_identity) as identity, \
                        mock.patch.object(do.worktree, 'create') as create:
                    for name, child in (('access', access), ('route', routing), ('identity', identity), ('create', create)):
                        order.attach_mock(child, name)
                    result = self.invoke()
                self.assertEqual([call[0] for call in order.mock_calls], ['access', 'route', 'identity'])
                identity.assert_called_once_with(self.repo)
                self.assertEqual((result['verdict'], result['exit_code']), ('blocked', exitcodes.FAIL), result)
                self.assertIn('configure git user.name and user.email (git config --global user.name', result['message'])
                self.assertNotIn('land_commands', result)
                self.assert_no_executor()
                self.assertFalse((self.root / 'repo-wt').exists())
                self.assertFalse((self.repo / '.orcexe/do.lock').exists())
                records = list(self.log().records(task=result['task_id']))
                self.assertEqual([r['event'] for r in records], ['task.classified', 'task.state'])
                self.assertEqual(records[-1]['data'],
                                 {'from': 'draft', 'to': 'blocked', 'reason': 'git_identity_missing'})
                self.assertTrue(self.log().verify()['ok'])
                if present:
                    self.git('config', '--unset', 'user.' + present)

    def test_c12_identity_is_resolved_only_before_executor(self):
        config = self.home / '.gitconfig'
        config.write_text('[user]\nname = Before Executor\nemail = before@example.invalid\n')
        self.env['GIT_CONFIG_GLOBAL'] = str(config)
        for field in ('name', 'email'):
            self.git('config', '--unset', 'user.' + field)
        for key in tuple(self.env):
            if key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_')):
                del self.env[key]
        def change_global(wt):
            config.write_text('[user]\nname = After Executor\nemail = after@example.invalid\n')
        with self.executor(after=change_global), \
                mock.patch.object(do, '_resolve_identity', wraps=do._resolve_identity) as identity:
            result = self.invoke()
        identity.assert_called_once_with(self.repo)
        self.assertEqual(result['verdict'], 'accepted', result)
        ref = 'refs/orcexe/accepted/' + result['task_id']
        self.assertEqual(self.git('show', '-s', '--format=%an|%ae|%cn|%ce', ref),
                         'Before Executor|before@example.invalid|Before Executor|before@example.invalid')

    def test_l2_status_write_failure_never_records_accepted(self):
        def evil(wt):
            (next((self.repo / '.orcexe/runs').iterdir()) / 'status.md').mkdir()
        with self.executor(after=evil):
            result = self.invoke()
        self.assertNotEqual(result['exit_code'], exitcodes.OK)
        states = [r['data'] for r in self.log().records(event='task.state')]
        self.assertNotIn('accepted', [s['to'] for s in states])
        self.assertIn('report_failed', [s['reason'] for s in states])

    def test_l2_invalid_model_constraints_are_usage(self):
        for constraint in ([], 'x', {'force': 5}, {'unknown': 1}, {'tools': 'codex'}):
            with self.subTest(constraint=constraint):
                result = self.invoke(model_constraint=constraint)
                self.assertEqual(result['exit_code'], exitcodes.USAGE, result)
                self.assertIn('--model-constraint', result['message'])
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit) as error:
                cli.main(['do', FAST, '--model-constraint', '{'])
            self.assertEqual(error.exception.code, exitcodes.USAGE)
        self.assert_no_executor()
        self.assertFalse((self.repo / '.orcexe').exists())

    def test_l2_symlink_orcexe_is_refused(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        pointer = self.repo / '.orcexe'
        for target in (elsewhere, self.root / 'missing'):
            pointer.symlink_to(target)
            result = self.invoke()
            self.assertEqual(result['exit_code'], exitcodes.FAIL, result)
            self.assertIn('symlink', result['message'])
            self.assert_no_executor()
            self.assertEqual(list(elsewhere.iterdir()), [])
            pointer.unlink()

    def test_l2_d1_fresh_dead_lock_reclaimed_with_abandoned_state(self):
        # Retain the actual owned dispatch path and task id for recovery output.
        result = self.invoke()
        old_task = result['task_id']
        lock = self.repo / '.orcexe/do.lock'
        lock.write_text(json.dumps(dict(pid=2147483646, task_id=old_task,
            created_utc=datetime.now(timezone.utc).isoformat(), host=socket.gethostname())))
        result2 = self.invoke()
        self.assertEqual(result2['verdict'], 'accepted', result2)
        self.assertIn(result['worktree'], result2['message'])
        self.assertIn('kill -9', result2['message'])
        states = list(self.log().records(task=old_task, event='task.state'))
        self.assertEqual(states[-1]['data'], {'from': 'running', 'to': 'failed', 'reason': 'abandoned'})
        self.assertFalse(lock.exists())

    def test_l2_live_lock_names_pid_task_and_host(self):
        lock = self.repo / '.orcexe/do.lock'
        lock.parent.mkdir()
        lock.write_text(json.dumps(dict(pid=os.getpid(), task_id='do-running',
            created_utc=datetime.now(timezone.utc).isoformat(), host=socket.gethostname())))
        result = self.invoke()
        self.assertIn('pid ' + str(os.getpid()), result['message'])
        self.assertIn('task do-running', result['message'])
        self.assert_no_executor()

    @unittest.skipUnless(os.name == 'posix', 'POSIX signals')
    def test_l2_term_hup_int_cancel_executor_and_release_lock(self):
        for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
            with self.subTest(sig=sig):
                self.signal_run(sig)

    @unittest.skipUnless(os.name == 'posix', 'POSIX signals')
    def test_l2_kill9_reclaims_immediately_and_reports_residual_executor(self):
        self.signal_run(signal.SIGKILL)

    def signal_run(self, sig):
        ready = self.root / ('pid-' + str(sig))
        # Wrap proc.run's exec call while the shipped fake really runs.
        script = '''import json,os,sys
from pathlib import Path
from unittest import mock
from orcexe import do,proc
from orcexe.adapters.codex import CodexAdapter
ready=Path(sys.argv[2]); real=proc.run; popen=proc.subprocess.Popen
def spawn(*args,**kwargs):
    child=popen(*args,**kwargs); ready.write_text(str(child.pid)); return child
def run(argv,**kwargs):
    if len(argv)>1 and argv[1]=='exec':
        with mock.patch.object(proc.subprocess,'Popen',side_effect=spawn):
            return real(argv,**kwargs)
    return real(argv,**kwargs)
env=dict(os.environ)
def adapter():
    return CodexAdapter(env=env,allow_extra=tuple(k for k in env if k.startswith('ORCEXE_FAKE_')),
        detect_timeout_s=10,appserver_timeout_s=10,auth_timeout_s=10,kill_grace_s=.15)
with mock.patch.object(do,'CodexAdapter',side_effect=adapter),mock.patch.object(proc,'run',side_effect=run):
    print(json.dumps(do.run_do('Add a docstring to src/app.py',sys.argv[1],timeout=60)),flush=True)
'''
        env = dict(self.env, PYTHONPATH=str(ROOT / 'plugins/orcexe/src'),
                   ORCEXE_FAKE_SCENARIO='stall', ORCEXE_FAKE_SLEEP='55')
        parent = subprocess.Popen([sys.executable, '-c', script, str(self.repo), str(ready)], env=env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child_pid = None
        try:
            end = time.monotonic() + scaled(15)
            while not ready.exists() and time.monotonic() < end and parent.poll() is None:
                time.sleep(.02)
            self.assertTrue(ready.exists())
            child_pid = int(ready.read_text())
            lock = self.repo / '.orcexe/do.lock'
            stored = json.loads(lock.read_text())
            self.assertEqual(set(stored), {'pid', 'created_utc', 'task_id', 'host'})
            parent.send_signal(sig)
            output, err = parent.communicate(timeout=scaled(15))
            if sig == signal.SIGKILL:
                self.assertEqual(parent.returncode, -sig)
                os.kill(child_pid, 0)  # Documented residual process survives kill -9.
                result = self.invoke()
                self.assertEqual(result['exit_code'], exitcodes.OK, result)
                self.assertIn('Abandoned task ' + stored['task_id'], result['message'])
                states = list(self.log().records(task=stored['task_id'], event='task.state'))
                self.assertEqual(states[-1]['data']['reason'], 'abandoned')
            else:
                self.assertEqual(parent.returncode, 0, err)
                result = json.loads(output)
                self.assertEqual((result['verdict'], result['exit_code']), ('cancelled', exitcodes.FAIL), result)
                self.assertEqual(result['signal_exit_code'], 128 + sig)
                self.assertNotIn('land_commands', result)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)
                states = list(self.log().records(task=stored['task_id'], event='task.state'))
                self.assertEqual(states[-1]['data']['reason'], 'signal_' + sig.name)
                rd = self.repo / '.orcexe/runs' / (stored['task_id'] + '-r1')
                self.assertIn('Verdict: cancelled', (rd / 'status.md').read_text())
            self.assertFalse(lock.exists())
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.communicate(timeout=scaled(10))
            if child_pid:
                try:
                    os.killpg(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_l2_signal_handlers_restored(self):
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP) if os.name == 'posix' else ()
        before = {sig: signal.getsignal(sig) for sig in signals}
        self.invoke()
        self.assertEqual({sig: signal.getsignal(sig) for sig in signals}, before)

    def test_l2_snapshot_excludes_tracked_orcexe_but_keeps_main_renames(self):
        folder = self.repo / '.orcexe'
        folder.mkdir()
        (folder / 'state.txt').write_text('before\n')
        self.git('add', '.orcexe/state.txt')
        self.git('commit', '-qm', 'tracked internal state')
        with mock.patch.dict(os.environ, self.env, clear=True):
            before = do._main_snapshot(self.repo, 'do-task')
            (folder / 'state.txt').write_text('after\n')
            (folder / 'new.txt').write_text('ignored by observer\n')
            self.assertEqual(do._main_snapshot(self.repo, 'do-task'), before)
            self.git('mv', 'README.md', 'README-new.md')
            self.assertNotEqual(do._main_snapshot(self.repo, 'do-task'), before)

    def test_l2_freeze_preserves_mode_identity_and_rejects_ref_overwrite(self):
        self.git('update-index', '--chmod=+x', 'src/app.py')
        self.git('commit', '-qm', 'executable source')
        self.git('config', 'author.name', 'Configured Author')
        self.git('config', 'author.email', 'author@example.invalid')
        self.git('config', 'committer.name', 'Configured Committer')
        self.git('config', 'committer.email', 'committer@example.invalid')
        for key in tuple(self.env):
            if key.startswith(('GIT_AUTHOR_', 'GIT_COMMITTER_')):
                del self.env[key]
        result = self.invoke()
        self.assertEqual(result['verdict'], 'accepted', result)
        ref = 'refs/orcexe/accepted/' + result['task_id']
        self.assertEqual(self.git('show', '-s', '--format=%an|%ae|%cn|%ce', ref),
            'Configured Author|author@example.invalid|Configured Committer|committer@example.invalid')
        self.assertTrue(self.git('ls-tree', ref, 'src/app.py').startswith('100755 blob '))
        receipt = json.loads((self.repo / '.orcexe/runs' / result['run_id'] / 'gate.json').read_text())
        wt = next(w for w in worktree.list_managed(self.repo) if w.path == result['worktree'])
        commit = self.git('rev-parse', ref)
        with mock.patch.dict(os.environ, self.env, clear=True), self.assertRaises(do._DoFailure):
            do._accepted_commit(self.repo, wt, receipt, result['task_id'], 'other message', 'src/app.py',
                                do._resolve_identity(self.repo))
        self.assertEqual(self.git('rev-parse', ref), commit)
