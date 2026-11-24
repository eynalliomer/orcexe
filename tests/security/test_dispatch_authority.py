"""Offline behavioural proofs for the dispatch-authority classes (R9)."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import unittest
from unittest import mock

from tests.unit.test_gitutil import GitFixture
from tests.unit.test_task import TaskFixture
from tests.unit.test_planning import task as plan_task
from tests.unit.test_planrun import PlanRunTests
from tests.unit.test_risky_lane import RiskyFixture, audit
from tests.unit.test_task_l1 import rubric
from orcexe import cli, isolation, ledger, runguard
from orcexe.adapters import ClaudeCodeAdapter, CodexAdapter, CursorAdapter


class LedgerCASAuthorityTests(GitFixture):
    def test_concurrent_create_and_move_never_overwrite_the_foreign_head(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            log = ledger.Ledger(ledger.default_path(self.main))
            real = ledger._git
            for create in (True, False):
                with self.subTest(create=create):
                    if not create:
                        # Establish a legitimate head after the concurrent-create case.
                        self.assertEqual(real(self.main, 'update-ref', '-d', ledger.HEAD_REF)[0], 0)
                        log.append('task.state', 'T1', {'from': 'pending', 'to': 'running', 'reason': 'fixture'})
                    before = log.path.read_bytes() if log.path.exists() else b''
                    foreign, swaps = [], []

                    def racing(repo, *args, **kwargs):
                        if 'update-ref' in args and ledger.HEAD_REF in args:
                            index = args.index(ledger.HEAD_REF)
                            swaps.append(args[index + 1:])
                            oid = real(repo, 'hash-object', '-w', '--stdin', data=b'concurrent head\n')[1].decode().strip()
                            foreign.append(oid)
                            self.assertEqual(real(repo, 'update-ref', ledger.HEAD_REF, oid)[0], 0)
                        return real(repo, *args, **kwargs)

                    with mock.patch.object(ledger, '_git', side_effect=racing):
                        with self.assertRaises(ledger.HeadMismatch) as caught:
                            log.append('task.state', 'T2', {'from': 'pending', 'to': 'running', 'reason': 'fixture'})
                    self.assertIn(caught.exception.detail, ('head_moved', 'ledger_head_mismatch'))
                    self.assertEqual(len(swaps), 1)
                    self.assertEqual(len(swaps[0]), 2, 'actual argv must contain new and old oid')
                    if create:
                        self.assertEqual(swaps[0][1], '0' * len(swaps[0][0]))
                    self.assertEqual(real(self.main, 'rev-parse', ledger.HEAD_REF)[1].decode().strip(), foreign[0])
                    self.assertEqual(log.path.read_bytes(), before, 'refused append must roll back')


class RunGuardAuthorityTests(GitFixture):
    def setUp(self):
        super().setUp()
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        self.log = ledger.Ledger(ledger.default_path(self.main))
        self.log.append('task.state', 'T1', {'from': 'pending', 'to': 'running', 'reason': 'fixture'})
        self.runs = self.main / '.orcexe/runs'
        self.own = self.runs / 'task-1/attempt-1'
        self.own.mkdir(parents=True)

    def assert_tamper(self, call):
        with ledger.session():
            self.log.verify_head()
            trusted = ledger.trusted(self.log.path)
            guard = runguard.Guard(self.main, self.runs, self.log)
            with self.assertRaises(isolation.StateTampered):
                guard.run(call, self.own)
            with self.assertRaises(isolation.StateTampered):
                guard.check()
            self.assertEqual(ledger.read_marker(self.main / '.orcexe'), (True, trusted))

    def test_foreign_ledger_change_is_terminal(self):
        def write():
            with self.log.path.open('ab') as handle:
                handle.write(b'foreign\n')
        self.assert_tamper(write)

    def test_foreign_head_change_is_terminal(self):
        self.assert_tamper(lambda: self.git_run(self.main, 'update-ref', '-d', ledger.HEAD_REF))

    def test_foreign_run_directory_change_is_terminal(self):
        self.assert_tamper(lambda: (self.runs / 'other-task').mkdir())

    def test_every_declared_adapter_output_is_allowed(self):
        guard = runguard.Guard(self.main, self.runs, self.log)
        def write():
            for name in sorted(runguard.ADAPTER_OUTPUTS):
                path = self.own / name
                if name == 'brief':
                    path.mkdir()
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b'output\n')
            return 'receipt'
        self.assertEqual(guard.run(write, self.own), 'receipt')
        guard.check()
        self.assertFalse((self.main / runguard.MARKER).exists())

    def test_control_files_cannot_be_created_removed_or_rewritten(self):
        for name in ('do.lock', 'do.reclaim'):
            path = self.main / '.orcexe' / name
            for operation in ('create', 'remove', 'rewrite'):
                for with_main in (False, True):
                    with self.subTest(name=name, operation=operation, with_main=with_main):
                        if path.exists():
                            path.unlink()
                        if operation != 'create':
                            path.write_bytes(b'original\n')
                        guard = runguard.Guard(self.main, self.runs, self.log)
                        call = path.unlink if operation == 'remove' else lambda: path.write_bytes(b'foreign\n')
                        with self.assertRaises(isolation.StateTampered):
                            guard.run(call, self.own, with_main=with_main)
                        with self.assertRaises(isolation.StateTampered):
                            guard.check()
                        (self.main / runguard.MARKER).unlink()

    @unittest.skipUnless(hasattr(os, 'mkfifo'), 'FIFOs unavailable')
    def test_a_control_file_replaced_by_a_fifo_is_terminal_without_blocking(self):
        for name in ('do.lock', 'do.reclaim'):
            with self.subTest(name=name):
                path = self.main / '.orcexe' / name
                # An empty FIFO must not compare equal to an empty regular file.
                path.write_bytes(b'')
                def replace():
                    path.unlink()
                    os.mkfifo(str(path))
                reads, real_open = [], os.open
                def open_control(target, flags, *args, **kwargs):
                    if Path(target) == path and flags & os.O_ACCMODE == os.O_RDONLY:
                        reads.append(flags)
                        # Fail promptly even if O_NONBLOCK is later deleted.
                        if not flags & os.O_NONBLOCK:
                            raise ValueError('blocking control-file observation')
                    return real_open(target, flags, *args, **kwargs)
                with mock.patch.object(runguard.os, 'open', side_effect=open_control):
                    self.assert_tamper(replace)
                self.assertGreaterEqual(len(reads), 2)
                self.assertTrue(all(flags & os.O_NONBLOCK for flags in reads))
                path.unlink()
                (self.main / runguard.MARKER).unlink()


class ExecutorAuthorityTests(TaskFixture):
    def test_plan_runner_still_acquires_and_releases_its_own_lock(self):
        self.saved = self.save([plan_task()])
        self.actions.write_text(json.dumps([{'README.md': 'Corrected spelling\n'}]))
        lock = self.main / '.orcexe/do.lock'
        observed = []
        def check(request):
            self.assertTrue(lock.is_file())
            observed.append(lock.read_bytes())
        code, result, error = PlanRunTests.invoke_plan(self, before=check)
        self.assertEqual((code, result['state'], error), (0, 'completed', ''))
        self.assertEqual(len(observed), 1)
        self.assertFalse(lock.exists())
        self.assertFalse((self.main / runguard.MARKER).exists())

    def test_executor_writing_another_run_is_terminal_and_never_landable(self):
        self.saved = self.save([plan_task()])
        self.actions.write_text(json.dumps([{'README.md': 'Corrected spelling\n'}]))
        def write(request):
            foreign = self.main / '.orcexe/runs/foreign-executor'
            foreign.mkdir()
            (foreign / 'result.json').write_text('{}')
        with mock.patch.object(isolation, 'evidence', return_value='seatbelt'):
            code, result, error = self.invoke(before=write)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((code, result['state'], result['reason'], result['land']),
                         (1, 'rejected', 'state_tampered', None), (result, error))
        self.assertTrue((self.main / runguard.MARKER).is_file())
        self.assertEqual(self.git_run(self.main, 'for-each-ref', 'refs/orcexe/accepted/').strip(), '')


class DeferredAuthorityTests(TaskFixture):
    def test_standard_task_plan_run_and_resume_never_dispatch_deferred_work(self):
        self.saved = self.save([plan_task('T1', lane='standard'), plan_task('T2')], held=['T1'])
        self.assertEqual(self.saved['approval']['approved'], ['T2'])
        # A previously rejected approved task lets the runner visit the deferred
        # task without any runnable work or model calls. Empty approvals are invalid.
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.log().append('task.state', 'T2', dict(plan_id=self.saved['plan_id'],
                **{'from': 'pending', 'to': 'rejected'}, reason='fixture'))
        runs = self.main / '.orcexe/runs'
        before = runguard.fingerprint(runs)
        with mock.patch.object(CodexAdapter, 'run', side_effect=AssertionError('model called')) as codex, \
                mock.patch.object(ClaudeCodeAdapter, 'run', side_effect=AssertionError('model called')) as claude, \
                mock.patch.object(CursorAdapter, 'run', side_effect=AssertionError('model called')) as cursor:
            code, result, error = self.invoke()
            self.assertEqual((code, result['reason'], result['attempts']), (2, 'task_deferred', 0), (result, error))
            for resume in (False, True):
                out, err = io.StringIO(), io.StringIO()
                args = ['plan', '--run', self.saved['plan_id'], '--repo', str(self.main), '--json']
                if resume:
                    args.append('--resume')
                with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out), redirect_stderr(err), \
                        mock.patch('orcexe.task.run_task', side_effect=AssertionError('deferred task dispatched')) as dispatch:
                    code = cli.main(args)
                result = json.loads(out.getvalue())
                self.assertEqual(code, 1, (result, err.getvalue()))
                self.assertEqual([(t['state'], t['attempts']) for t in result['tasks']], [('deferred', 0), ('rejected', 0)])
                dispatch.assert_not_called()
            for adapter in (codex, claude, cursor):
                adapter.assert_not_called()
        self.assertEqual(runguard.fingerprint(runs), before)
        self.assertFalse((self.main / '.orcexe/do.lock').exists(), 'CLI releases its own lock')


class RiskyDeferredAuthorityTests(RiskyFixture):
    def test_risky_task_and_contract_approval_refuse_a_deferred_task(self):
        self.saved = self.save(self.saved['tasks'] + [plan_task('T2')], held=['T1'])
        self.plan_id = self.saved['plan_id']
        runs = self.main / '.orcexe/runs'
        before = runguard.fingerprint(runs)
        with mock.patch.object(CodexAdapter, 'run') as executor, \
                mock.patch.object(ClaudeCodeAdapter, 'run') as reviewer, \
                mock.patch.object(CursorAdapter, 'run') as cursor:
            code, result, error = self.dispatch()
            self.assertEqual((code, result['reason'], result['attempts']), (2, 'task_deferred', 0), (result, error))
            code, out, error = self.approve_contract('0' * 64)
            self.assertEqual(code, 2, (out, error))
            self.assertIn('task_deferred', error)
            for adapter in (executor, reviewer, cursor):
                adapter.assert_not_called()
        self.assertEqual(runguard.fingerprint(runs), before)


class ReviewerAuthorityTests(RiskyFixture):
    def check_reviewer(self, stage, target):
        self.evaluator([rubric(), audit()])
        self.propose_and_approve()
        real = ClaudeCodeAdapter.run
        observed = []
        def run(adapter, req):
            if stage in req.run_id:
                observed.append(req.run_id)
                if target == 'ledger':
                    with (self.main / '.orcexe/ledger.jsonl').open('ab') as handle:
                        handle.write(b'foreign\n')
                elif target == 'plan':
                    with (self.main / '.orcexe/plan.json').open('ab') as handle:
                        handle.write(b' \n')
                else:
                    lock = self.main / '.orcexe' / target
                    lock.write_bytes(b'foreign\n')
            return real(adapter, req)
        with mock.patch.object(ClaudeCodeAdapter, 'run', new=run):
            code, result, error = self.dispatch()
        self.assertEqual(len(observed), 1, 'the selected reviewer must actually run')
        reason = 'main_repository_changed' if target == 'plan' else 'state_tampered'
        self.assertEqual((code, result['state'], result['reason'], result['land']),
                         (1, 'rejected', reason, None), (result, error))
        self.assertEqual(self.accepted_refs(), '')
        self.assertEqual((self.main / runguard.MARKER).exists(), target != 'plan')

    def test_l1_foreign_ledger_write_is_terminal_even_if_evaluate_swallows(self):
        self.check_reviewer('-l1', 'ledger')

    def test_auditor_foreign_ledger_write_is_terminal_even_if_audit_swallows(self):
        self.check_reviewer('-audit', 'ledger')

    def test_l1_uses_main_observation(self):
        self.check_reviewer('-l1', 'plan')

    def test_auditor_uses_main_observation(self):
        self.check_reviewer('-audit', 'plan')

    def test_l1_cannot_overwrite_the_lock(self):
        self.check_reviewer('-l1', 'do.lock')

    def test_auditor_cannot_create_reclaim(self):
        self.check_reviewer('-audit', 'do.reclaim')


def load_tests(loader, standard_tests, pattern):
    # Fixture imports do not pull the unit suite into the security gate.
    suite = unittest.TestSuite()
    for cls in (LedgerCASAuthorityTests, RunGuardAuthorityTests, ExecutorAuthorityTests,
                DeferredAuthorityTests, RiskyDeferredAuthorityTests, ReviewerAuthorityTests):
        suite.addTests(cls(name) for name in cls.__dict__ if name.startswith('test_'))
    return suite
