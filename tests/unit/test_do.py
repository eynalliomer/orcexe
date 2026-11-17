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
from tests.unit._fixtures import GitTemplates, git_environment

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'plugins/orcexe/src'))
from orcexe import classify, cli, do, exitcodes, gate_lite, ledger, proc, route, worktree
from orcexe.adapters.codex import CodexAdapter
from orcexe.adapters.claude_code import ClaudeCodeAdapter
from orcexe.adapters.cursor import CursorAdapter
from orcexe.adapters.base import RunRequest

FAKE = ROOT / 'tests/fixtures/fake-cli/codex'
FAST = 'Add a docstring to src/app.py'
EVENTS = ['task.classified', 'task.dispatched', 'orchestrator.usage',
          'attempt.ended', 'gate.result', 'task.state']
EXTEND = ' " Added documentation."\n'


class DoFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.templates = GitTemplates(cls.addClassCleanup)
        cls.executor_env = dict(
            PATH=os.pathsep.join([str(Path(sys.executable).parent), os.defpath]),
            ORCEXE_BIN_CODEX=str(FAKE), ORCEXE_FAKE_SCENARIO='success',
            ORCEXE_FAKE_EDIT_FILE='src/app.py', ORCEXE_FAKE_EDIT_TEXT=EXTEND,
            PYTHONDONTWRITEBYTECODE='1', GIT_CONFIG_NOSYSTEM='1',
            GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
            GIT_AUTHOR_NAME='Test', GIT_AUTHOR_EMAIL='test@example.invalid',
            GIT_COMMITTER_NAME='Test', GIT_COMMITTER_EMAIL='test@example.invalid')


    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='orcexe-do-', dir=str(ROOT))
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root / 'repo'
        self.home = self.root / 'home'
        self.home.mkdir()
        self.calls = self.root / 'calls.jsonl'
        self.env = dict(self.executor_env, HOME=str(self.home),
            CODEX_HOME=str(self.home / 'codex'), XDG_CACHE_HOME=str(self.home / 'cache'),
            CLAUDE_CONFIG_DIR=str(self.home / 'claude'), ORCEXE_STATE_DIR=str(self.home / 'state'),
            ORCEXE_BIN_CLAUDE_CODE=str(FAKE.with_name('claude')),
            ORCEXE_BIN_CURSOR=str(FAKE.with_name('agent')), CURSOR_CONFIG_DIR=str(self.home / "cursor"),
            ORCEXE_FAKE_CALLS=str(self.calls))
        for name in ('SystemRoot', 'SYSTEMROOT', 'WINDIR'):
            if name in os.environ:
                self.env[name] = os.environ[name]
        git_environment(self.env, self.root)
        self.templates.copy('do', self.repo, self.build_repository)
        self.original = (self.repo / 'src/app.py').read_bytes()
        self.claude_scenario = "auth-missing"
        self.cursor_scenario = "auth-missing"
        self.factory = do.CodexAdapter
        self.gate = gate_lite.gate
        self.addCleanup(mock.patch.stopall)
        mock.patch('builtins.input', side_effect=AssertionError('stdin read')).start()


    def build_repository(self, directory):
        self.git('init', '-q', '--template=', cwd=directory)
        self.git('config', 'user.name', 'Test', cwd=directory)
        self.git('config', 'user.email', 'test@example.invalid', cwd=directory)
        (directory / 'README.md').write_text('Example\n')
        (directory / 'src').mkdir()
        (directory / 'src/app.py').write_text('def app():\n    """Return nothing."""')
        (directory / 'tests').mkdir()
        (directory / 'tests/__init__.py').touch()
        (directory / 'tests/test_app.py').write_text(
            'import unittest\nfrom src.app import app\n'
            'class AppTest(unittest.TestCase):\n'
            '    def test_app(self):\n        self.assertIsNone(app())\n')
        self.git('add', '.', cwd=directory)
        self.git('commit', '-qm', 'initial', cwd=directory)


    def git(self, *args, cwd=None):
        result = subprocess.run(['git', *args], cwd=str(cwd or self.repo), env=self.env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=scaled(10))
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()


    def adapter(self):
        return self.factory(env=self.env, allow_extra=tuple(k for k in self.env if k.startswith('ORCEXE_FAKE_')) + ('ORCEXE_FAKE_APPSERVER_SCENARIO',),
            detect_timeout_s=scaled(3), appserver_timeout_s=scaled(3), auth_timeout_s=scaled(3),
            kill_grace_s=scaled(.15))


    def claude_adapter(self):
        return ClaudeCodeAdapter(env=dict(self.env, ORCEXE_FAKE_SCENARIO=self.claude_scenario),
            allow_extra=tuple(k for k in self.env if k.startswith("ORCEXE_FAKE_")),
            detect_timeout_s=scaled(3), kill_grace_s=scaled(.15))


    def cursor_adapter(self):
        return CursorAdapter(env=dict(self.env, ORCEXE_FAKE_SCENARIO=self.cursor_scenario),
            allow_extra=tuple(k for k in self.env if k.startswith("ORCEXE_FAKE_")),
            detect_timeout_s=scaled(3), kill_grace_s=scaled(.15))


    def invoke(self, task=FAST, **options):
        # Keep production clocks real; scale every test-supplied deadline.
        def gate(**kwargs):
            return self.gate(command_timeout_s=scaled(10), total_timeout_s=scaled(20), **kwargs)
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(do, 'CodexAdapter', side_effect=self.adapter), \
                mock.patch.object(do, 'ClaudeCodeAdapter', side_effect=self.claude_adapter), \
                mock.patch.object(do, 'CursorAdapter', side_effect=self.cursor_adapter), \
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
    child=popen(*args,**kwargs)
    pending=ready.with_name(ready.name+'.tmp')
    pending.write_text(str(child.pid)); pending.replace(ready)
    return child
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



class DoTests(DoFixture):
    def test_cursor_fallback_when_codex_and_claude_unavailable(self):
        self.env["ORCEXE_BIN_CODEX"] = str(self.root / "missing-codex")
        self.env["ORCEXE_BIN_CLAUDE_CODE"] = str(self.root / "missing-claude")
        self.cursor_scenario = "success"
        result = self.invoke()
        self.assertEqual((result["verdict"], result["exit_code"]), ("accepted", 0), result)
        executions = [c for c in self.call_log() if c["model_call"]]
        self.assertEqual(len(executions), 1)
        self.assertIn("composer-2.5", executions[0]["argv"])
        self.assertEqual(executions[0]["argv"][0], "-p")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.repo / "src/app.py").read_bytes(), self.original)


    def test_c11_english(self):
        self.check(self.invoke(), 'accepted', exitcodes.OK)


    def test_codex_auth_missing_routes_to_claude_and_is_accepted(self):
        self.env['ORCEXE_FAKE_SCENARIO'] = 'auth-missing'
        self.claude_scenario = 'success'
        result = self.invoke()
        self.assertEqual((result['verdict'], result['exit_code']), ('accepted', exitcodes.OK), result)
        self.assertEqual(result['route']['tool'], 'claude-code')
        self.assertEqual(result['route']['confidence'], 'low')
        self.assertIn('access:not_seen', result['route']['basis'])
        executions = [c for c in self.call_log() if c['model_call']]
        self.assertEqual(len(executions), 1)
        self.assertEqual(executions[0]['argv'][0], '-p')
        self.assertEqual(executions[0]['cwd'], result['worktree'])
        self.assertEqual(result['changed_files'], ['src/app.py'])
        self.assertEqual((self.repo / 'src/app.py').read_bytes(), self.original)
        self.assertTrue(self.log().verify()['ok'])


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
                         {'quota_exhausted', 'model_unavailable', 'auth_missing'})
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
