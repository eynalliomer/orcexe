"""Repository safety, identity and signals using isolated fake executors."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import os
from pathlib import Path
import secrets
import shutil
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


from tests.unit.test_do import DoFixture


class DoSafetyTests(DoFixture):
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


    def test_l2_a4b_uncommitted_merge_lands_verified_bytes(self):
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
        result = self.invoke()
        self.assertEqual(result['verdict'], 'accepted', result)
        self.assertIn('merge --ff-only', result['land_commands'][0])
        self.land(result, self.original + EXTEND.encode())


    def test_l2_a4c_main_advanced_cherry_pick_lands_verified_bytes(self):
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
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
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
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
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
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
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
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
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
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
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
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
        # T55 (P2-2): during the executor this is a run-directory change, state_tampered.
        with self.executor(after=evil):
            result = self.invoke()
        self.assertEqual((result['exit_code'], result['reason']), (exitcodes.FAIL, 'state_tampered'), result)
        states = [r['data'] for r in self.log().records(event='task.state')]
        self.assertNotIn('accepted', [s['to'] for s in states])
        self.assertEqual([s['reason'] for s in states][-1], 'state_tampered')
        # The same unwritable report after the executor still never records accepted.
        shutil.rmtree(next((self.repo / '.orcexe/runs').iterdir()))
        (self.repo / '.orcexe/state-tampered').unlink(missing_ok=True)
        real = self.gate
        def gate(**kwargs):
            (Path(kwargs['run_dir']) / 'status.md').mkdir()
            return real(**kwargs)
        self.gate = gate
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
            with self.subTest(target=target):
                pointer.symlink_to(target, target_is_directory=True)
                result = self.invoke()
                self.assertEqual(result['exit_code'], exitcodes.USAGE, result)
                self.assertIn('.orcexe', result['message'])
                self.assertIn('symlink', result['message'])
                self.assert_no_executor()
                self.assertEqual(list(elsewhere.iterdir()), [])
                self.assertFalse((self.repo / '.orcexe' / 'ledger.jsonl').exists())
                pointer.unlink()


    def test_l2_orcexe_file_is_refused(self):
        (self.repo / '.orcexe').write_text('not a directory\n')
        result = self.invoke()
        self.assertEqual(result['exit_code'], exitcodes.USAGE, result)
        self.assertIn('.orcexe', result['message'])
        self.assert_no_executor()
        self.assertFalse((self.repo / '.orcexe' / 'ledger.jsonl').exists())


    def test_l2_nonexistent_repo_is_usage(self):
        missing = self.root / 'missing-repo'
        with mock.patch.dict(os.environ, self.env, clear=True):
            result = do.run_do(FAST, missing, timeout=scaled(60))
        self.assertEqual(result['exit_code'], exitcodes.USAGE, result)
        self.assertIn('repository', result['message'].lower())
        self.assert_no_executor()
        self.assertFalse((self.repo / '.orcexe').exists())


    def test_l2_surrogate_task_is_usage_without_ledger(self):
        result = self.invoke(task='\ud800')
        self.assertEqual(result['exit_code'], exitcodes.USAGE, result)
        self.assertIn('surrogates not allowed', result['message'])
        self.assert_no_executor()
        if (self.repo / '.orcexe' / 'ledger.jsonl').exists():
            self.assertEqual(list(self.log().records(event='task.classified')), [])
            self.assertEqual(list(self.log().records(event='task.state')), [])


    def test_l2_d1_fresh_dead_lock_reclaimed_with_abandoned_state(self):
        # Retain the actual owned dispatch path and task id for recovery output.
        result = self.invoke()
        old_task = result['task_id']
        lock = self.repo / '.orcexe/do.lock'
        lock.write_text(json.dumps(dict(pid=2147483646, task_id=old_task,
            created_utc=datetime.now(timezone.utc).isoformat(), host=socket.gethostname())))
        result2 = self.invoke()
        self.assertEqual(result2['verdict'], self.accepted_verdict, result2)
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


    def test_l2_signal_handlers_restored(self):
        signals = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP) if os.name == 'posix' else ()
        before = {sig: signal.getsignal(sig) for sig in signals}
        self.invoke()
        self.assertEqual({sig: signal.getsignal(sig) for sig in signals}, before)


    def test_l2_snapshot_binds_orcexe_config_and_keeps_main_renames(self):
        folder = self.repo / '.orcexe'
        folder.mkdir()
        (folder / 'state.txt').write_text('before\n')
        self.git('add', '.orcexe/state.txt')
        self.git('commit', '-qm', 'tracked internal state')
        with mock.patch.dict(os.environ, self.env, clear=True):
            before = do._main_snapshot(self.repo, 'do-task')
            (folder / 'state.txt').write_text('after\n')
            (folder / 'new.txt').write_text('new config\n')
            self.assertNotEqual(do._main_snapshot(self.repo, 'do-task'), before)
            before = do._main_snapshot(self.repo, 'do-task')
            self.git('mv', 'README.md', 'README-new.md')
            self.assertNotEqual(do._main_snapshot(self.repo, 'do-task'), before)


    def test_l2_freeze_preserves_mode_identity_and_rejects_ref_overwrite(self):
        if self.accepted_verdict != 'accepted':
            self.skipTest('Landing requires an available OS sandbox')
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
