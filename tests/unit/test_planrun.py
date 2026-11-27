"""Stacked plans, ledger status and deterministic interruption/resume proofs."""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
from pathlib import Path
import signal
import unittest
import warnings
from unittest import mock

from tests.unit.test_task import TaskFixture, APP, TEST, ROOT
from tests.unit import test_task_l1
from tests.unit.test_task_l1 import rubric
from tests.unit.test_planning import task as plan_task
from orcexe import cli, do, fsio, isolation, planning, planrun, proc, route, task, task_l1, worktree
from orcexe.adapters import CodexAdapter


class PlanRunTests(TaskFixture):
    def invoke_plan(self, resume=False, before=None, accepted=True, identifier=None, run_statuses=()):
        statuses = iter(run_statuses)
        def factory():
            adapter = CodexAdapter(env=self.env, kill_grace_s=.1)
            real = adapter.run
            def run(request):
                self.requests.append(request)
                self.briefs.append(Path(request.brief_path).read_text())
                if before:
                    before(request)
                result = real(request)
                status = next(statuses, None)
                if status is not None:
                    result.update(status=status, exit_code=1)
                    task.safe_write_json(Path(request.run_dir) / 'result.json', result, mode=0o600)
                return result
            adapter.run = run
            return adapter
        out, err = io.StringIO(), io.StringIO()
        arguments = ['plan', '--run', identifier or self.saved['plan_id'], '--repo', str(self.main), '--json']
        if resume:
            arguments.append('--resume')
        with warnings.catch_warnings(), mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(task, 'CodexAdapter', side_effect=factory), \
                mock.patch.object(isolation, 'evidence', return_value='seatbelt' if accepted else 'none'), \
                redirect_stdout(out), redirect_stderr(err):
            # Exercise real project gates and fake tool wrappers on every host.
            # Acceptance evidence is simulated; OS policy has its own tests.
            warnings.simplefilter('ignore', DeprecationWarning)
            code = cli.main(arguments)
        return code, json.loads(out.getvalue()), err.getvalue()

    def status(self, repo=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['status', '--repo', str(repo or self.main), '--json'])
        self.assertEqual((code, err.getvalue()), (0, ''))
        return json.loads(out.getvalue())

    def docs(self, tasks):
        self.saved = self.save(tasks)
        self.actions.write_text(json.dumps([{'README.md': 'First accepted\n'}, {'README.md': 'Second accepted\n'},
                                            {'README.md': 'Third accepted\n'}]))

    def test_stacked_topological_order_dependency_visibility_and_finished_noop(self):
        # T3 is first in the plan, but T1/T2 are ready. Pick plan order anew
        # among ready tasks: T1, T3, T2 (not a batch of initial ready tasks).
        self.docs([plan_task('T3', depends_on=['T1']), plan_task('T1'), plan_task('T2')])
        bases = []
        def before(req):
            bases.append(self.git_run(req.workdir, 'rev-parse', 'HEAD').strip())
            if req.task == 'T3':
                self.assertEqual((Path(req.workdir) / 'README.md').read_text(), 'First accepted\n')
            if len(self.requests) > 1:
                previous = self.status()['tasks']
                self.assertEqual(sum(t['state'] == 'accepted' for t in previous), len(self.requests) - 1)
                text = (self.main / '.orcexe/status.md').read_text()
                self.assertIn('| accepted | 1 | accepted |', text)
            self.assertFalse((Path(req.workdir) / '.orcexe/status.md').exists())
        original = (self.main / 'README.md').read_bytes()
        index = (self.main / '.git/index').read_bytes()
        with mock.patch.object(task, '_read_plan', wraps=task._read_plan) as read:
            code, result, error = self.invoke_plan(before=before)
        # Validated once under the lock, plus (T59 r3, §4 P3-1) a re-check of the approval after
        # each dispatched task's executor window; run_task itself never re-reads the plan.
        self.assertEqual(read.call_count, 1 + len(self.requests))
        self.assertEqual((code, result['state'], error), (0, 'completed', ''), result)
        self.assertEqual([req.task for req in self.requests], ['T1', 'T3', 'T2'])
        self.assertEqual(self.git_run(self.main, 'rev-parse', 'HEAD').strip(), self.head)
        self.assertEqual((self.main / 'README.md').read_bytes(), original)
        self.assertEqual((self.main / '.git/index').read_bytes(), index)
        ref = result['land']['ref']
        self.assertTrue(ref.startswith('refs/orcexe/'))
        self.assertIn('merge --ff-only ' + ref, result['land']['command'])
        self.assertEqual(self.git_run(self.main, 'rev-parse', ref + '~3').strip(), self.head)
        self.assertEqual(self.git_run(self.main, 'show', ref + ':README.md'), 'Third accepted\n')
        self.assertEqual(bases, [self.git_run(self.main, 'rev-parse', ref + suffix).strip()
                                 for suffix in ('~3', '~2', '~1')])
        contracts = list(self.log().records(event='contract.frozen'))
        self.assertEqual([r['data']['head'] for r in contracts], bases)
        self.assertEqual(self.git_run(self.main, 'for-each-ref', '--format=%(refname)', 'refs/heads/' + ref), '')
        before_log = self.log().path.read_bytes()
        before_calls = self.calls.read_bytes()
        code, again, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, again), (0, result))
        self.assertEqual(self.log().path.read_bytes(), before_log)
        self.assertEqual(self.calls.read_bytes(), before_calls)
        code, again, _ = self.invoke_plan()
        self.assertEqual((code, again), (0, result))
        self.assertEqual(self.log().path.read_bytes(), before_log)
        status = self.status()
        self.assertEqual((status['schema'], status['plan_id'], status['plan_status']),
                         ('orcexe.status.v1', self.saved['plan_id'], 'approved'))
        self.assertEqual(status['tasks'], result['tasks'])
        self.assertEqual(status['land'], result['land'])
        summary = (self.main / '.orcexe/current-state.md').read_text()
        self.assertIn('Chain tip: ' + self.git_run(self.main, 'rev-parse', ref).strip(), summary)
        self.assertIn(result['land']['command'], summary)
        for item in status['tasks']:
            self.assertIn(item['task_id'] + ': ' + item['state'], summary)
        path = self.main / '.orcexe/status.md'
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        for item in result['tasks']:
            self.assertIn('| {} | fast | accepted | 1 | accepted |'.format(item['task_id']), path.read_text())

    def test_single_dependent_task_uses_same_integration_chain(self):
        self.docs([plan_task(), plan_task('T2', depends_on=['T1'])])
        with mock.patch.object(isolation, 'evidence', return_value='seatbelt'):
            code, first, _ = self.invoke()
            self.assertEqual((code, first['state']), (0, 'accepted'), first)
            first_commit = self.git_run(self.main, 'rev-parse', first['land']['ref']).strip()
            def before(req):
                self.assertEqual((Path(req.workdir) / 'README.md').read_text(), 'First accepted\n')
            code, second, _ = self.invoke(task_id='T2', before=before)
        self.assertEqual((code, second['state']), (0, 'accepted'), second)
        records = list(self.log().records(event='contract.frozen'))
        self.assertEqual(records[1]['data']['head'], first_commit)
        self.assertEqual(self.git_run(self.main, 'rev-parse', planrun.chain_ref(self.saved) + '~2').strip(), self.head)
        self.assertEqual(self.git_run(self.main, 'rev-parse', 'HEAD').strip(), self.head)


    # T60: risky is runnable (K18 a); this T59 test keeps lane_not_supported via the K18 (b) fallback.
    @mock.patch.object(planning, 'SUPPORTED_LANES', ('fast', 'standard'))
    def test_failure_blocks_only_transitive_dependents_and_risky_lanes(self):
        self.docs([plan_task('T3', depends_on=['T2']), plan_task('T1'),
                   plan_task('T2', depends_on=['T1']), plan_task('T4'),
                   plan_task('T5', lane='risky'), plan_task('T6', depends_on=['T5'])])
        self.actions.write_text(json.dumps([{'src/forbidden.py': 'outside\n'}, {'README.md': 'Independent\n'}]))
        code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['state']), (1, 'partial'), result)
        states = {t['task_id']: (t['state'], t['reason'], t['attempts']) for t in result['tasks']}
        self.assertEqual(states, {'T1': ('rejected', 'outside_allow', 1),
            'T2': ('blocked', 'dependency_failed', 0), 'T3': ('blocked', 'dependency_failed', 0),
            'T4': ('accepted', 'accepted', 1),
            # T59: the risky task and its dependant are deferred at approval, never dispatched.
            'T5': ('deferred', 'lane_not_supported', 0), 'T6': ('deferred', 'dependency_deferred', 0)})
        self.assertEqual([r.task for r in self.requests], ['T1', 'T4'])
        before = self.log().path.read_bytes()
        self.assertEqual(self.invoke_plan()[0], 1)
        self.assertEqual(self.log().path.read_bytes(), before)
        self.assertEqual(self.status()['tasks'], result['tasks'])


    def test_interrupt_closes_attempt_removes_worktree_and_resume_keeps_contract(self):
        self.docs([plan_task(), plan_task('T2', depends_on=['T1']), plan_task('T3', depends_on=['T2'])])
        interrupted_paths = []
        def interrupt(req):
            if req.task == 'T2':
                self.assertEqual(list(self.log().records(event='attempt.started'))[-1]['data']['attempt'], 1)
                self.assertEqual(self.status()['tasks'][1]['state'], 'running')
                interrupted_paths.append(req.workdir)
                os.kill(os.getpid(), signal.SIGTERM)
        code, result, error = self.invoke_plan(before=interrupt)
        self.assertEqual((code, result['state'], error), (130, 'interrupted', ''), result)
        self.assertEqual([t['state'] for t in result['tasks']], ['accepted', 'interrupted', 'pending'])
        self.assertFalse(Path(interrupted_paths[0]).exists())
        self.assertNotIn(interrupted_paths[0], [wt.path for wt in worktree.list_managed(self.main)])
        ended = list(self.log().records(task='T2', event='attempt.ended'))
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]['data']['status'], 'cancelled')
        self.assertFalse((self.main / '.orcexe/do.lock').exists())
        frozen = list(self.log().records(task='T2', event='contract.frozen'))[0]['data']
        contents = (self.main / frozen['path']).read_bytes()
        before_log = self.log().path.read_bytes()
        calls = len(self.requests)
        code, refused, error = self.invoke_plan()
        self.assertEqual((code, refused['reason'], error), (2, 'run_interrupted', 'not_seen: run_interrupted\n'))
        self.assertEqual(self.log().path.read_bytes(), before_log)
        self.assertEqual(len(self.requests), calls)
        code, resumed, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, resumed['state']), (0, 'completed'), resumed)
        self.assertEqual([t['attempts'] for t in resumed['tasks']], [1, 2, 1])
        self.assertEqual([req.task for req in self.requests], ['T1', 'T2', 'T2', 'T3'])
        self.assertEqual(len(list(self.log().records(task='T2', event='contract.frozen'))), 1)
        self.assertEqual((self.main / frozen['path']).read_bytes(), contents)
        self.assertEqual([r['data']['attempt'] for r in self.log().records(task='T2', event='attempt.started')], [1, 2])
        self.assertTrue(all(r['data']['contract_sha256'] == frozen['contract_sha256']
                            for r in self.log().records(task='T2', event='attempt.started')))
        self.assertEqual([r['data']['to'] for r in self.log().records(task='T2', event='task.state')],
                         ['interrupted', 'accepted'])
        self.assertEqual(self.status()['tasks'], resumed['tasks'])

    def test_land_form_lists_each_task_basis_and_the_weakest(self):
        claude = test_task_l1.EvaluatorTests.evaluator(self, [rubric()])
        codex = test_task_l1.codex_reviewer(self, [rubric()])
        standard = dict(lane='standard', files={'allow': ['src/*.py', 'tests/**']})
        self.saved = self.save([plan_task('T1', done=['app returns 2'], **standard),
                                plan_task('T2', done=['app returns 3'], depends_on=['T1'], **standard)])
        self.actions.write_text(json.dumps([
            {'src/app.py': APP.replace('1', '2'), 'tests/test_app.py': TEST.replace('1', '2')},
            {'src/app.py': APP.replace('1', '3'), 'tests/test_app.py': TEST.replace('1', '3')}]))
        roles = test_task_l1._roles(('claude-code', 'claude-sonnet-5-5'), ('codex', 'gpt-6.1-terra'))
        real = task_l1.evaluator_chain
        calls = []
        def chain(executor, adapters, *view):  # T60: the dispatch passes its one access snapshot
            value = real(executor, adapters, *view)
            calls.append(executor)
            # T1: the other family is unavailable, so only the same-family model is left.
            return [c for c in value if c['l1_basis'] == 'same_family'] if len(calls) == 1 else value
        with mock.patch.object(route, 'load_roles', return_value=roles), \
                mock.patch.object(task_l1, 'evaluator_chain', side_effect=chain):
            code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['state']), (0, 'completed'), result)
        self.assertEqual((claude.read_text(), codex.read_text()), ('1', '1'))
        expected = dict(tasks=[dict(task_id='T1', basis='same_family'), dict(task_id='T2', basis='cross_family')],
                        weakest='same_family')
        self.assertEqual(result['land']['l1_basis'], expected)
        self.assertEqual([(t['task_id'], t['l1_basis']) for t in result['tasks']],
                         [('T1', 'same_family'), ('T2', 'cross_family')])
        self.assertEqual(self.status()['land'], result['land'])
        text = (self.main / '.orcexe/status.md').read_text()
        self.assertIn('| T1 | standard | accepted | 1 | accepted | same_family |', text)
        self.assertIn('| T2 | standard | accepted | 1 | accepted | cross_family |', text)
        self.assertIn('weakest: same_family): T1=same_family, T2=cross_family', text)

    def test_fast_lane_acceptance_has_no_l1_basis(self):
        self.docs([plan_task()])
        code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['tasks'][0]['l1_basis']), (0, 'none'), result)
        self.assertEqual(result['land']['l1_basis'], dict(tasks=[dict(task_id='T1', basis='none')], weakest='none'))

    def test_resume_rebuilds_l1_tree_memory_from_ledger(self):
        reviews = test_task_l1.EvaluatorTests.evaluator(self, [rubric(False), rubric()])
        same = {'src/app.py': APP.replace('1', '2'), 'tests/test_app.py': TEST.replace('1', '2')}
        self.actions.write_text(json.dumps([same, same]))
        def interrupt(req):
            if req.run_id.endswith('-r2'):
                os.kill(os.getpid(), signal.SIGTERM)
        code, result, _ = self.invoke_plan(before=interrupt)
        self.assertEqual((code, result['tasks'][0]['state']), (130, 'interrupted'), result)
        self.assertEqual(reviews.read_text(), '1')
        with mock.patch.object(task, '_feedback', wraps=task._feedback) as feedback:
            code, resumed, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, resumed['tasks'][0]['state'], resumed['tasks'][0]['attempts']),
                         (1, 'exhausted', 3), resumed)
        self.assertEqual(reviews.read_text(), '1')   # the resumed attempt did not ask L1 again
        self.assertEqual(len(list(self.log().records(event='l1.started'))), 1)
        verdicts = [r['data'] for r in self.log().records(event='l1.verdict')]
        self.assertEqual([(v['attempt'], v['criteria'], v.get('reused_from')) for v in verdicts],
                         [(1, ['d1:fail'], None), (3, ['d1:fail'], verdicts[0]['run_id'])])
        self.assertEqual(verdicts[0]['tree'], verdicts[1]['tree'])
        # The remembered rejection carries the same feedback, read back from the stored rubric.
        checks = feedback.call_args[0][1]
        self.assertIn(dict(id='d1', status='fail', detail='missing edge case'), checks)
        self.assertTrue(self.log().verify()['ok'])

    def test_resume_never_replenishes_attempt_budget(self):
        self.docs([plan_task()])
        def interrupt(req):
            os.kill(os.getpid(), signal.SIGINT)
        for index in range(3):
            code, result, _ = self.invoke_plan(resume=index > 0, before=interrupt)
            self.assertEqual((code, result['tasks'][0]['attempts']), (130, index + 1), result)
        code, result, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, result['tasks'][0]['state'], result['tasks'][0]['attempts']), (1, 'exhausted', 3), result)
        self.assertEqual(len(self.requests), 3)
        self.assertEqual(len(list(self.log().records(event='contract.frozen'))), 1)
        self.assertEqual(len(list(self.log().records(event='attempt.ended'))), 3)

    def timeout_ended(self):
        # Reproduce a dispatch that ends after its timeout gate, before the next attempt.
        with mock.patch.object(task, '_feedback', side_effect=ValueError('timeout')):
            code, result, error = self.invoke_plan(run_statuses=('timeout',))
        self.assertEqual((code, result['tasks'][0]['attempts'], error), (1, 1, ''), result)
        terminal = list(self.log().records(event='task.state'))[-1]['data']
        self.assertEqual((terminal['to'], terminal['reason']), ('rejected', 'timeout'))
        return list(self.log().records(event='contract.frozen'))[-1]['data']

    def assert_timeout_resume(self, standard=False):
        if standard:
            test_task_l1.EvaluatorTests.evaluator(self, [rubric()])
        else:
            self.docs([plan_task()])
        frozen = self.timeout_ended()
        contents = (self.main / frozen['path']).read_bytes()
        self.assertEqual([(t['state'], t['reason'], t['attempts']) for t in self.status()['tasks']],
                         [('pending', 'timeout', 1)])
        before = self.log().path.read_bytes()
        code, unchanged, _ = self.invoke_plan()
        self.assertEqual((code, unchanged['tasks']), (1, self.status()['tasks']))
        self.assertEqual(self.log().path.read_bytes(), before)
        self.assertEqual(len(self.requests), 1)
        code, resumed, error = self.invoke_plan(resume=True)
        self.assertEqual((code, resumed['state'], error), (0, 'completed', ''), resumed)
        self.assertEqual((resumed['tasks'][0]['state'], resumed['tasks'][0]['attempts']), ('accepted', 2))
        self.assertEqual([r['data']['attempt'] for r in self.log().records(event='attempt.started')], [1, 2])
        self.assertTrue(all(r['data']['contract_sha256'] == frozen['contract_sha256']
                            for r in self.log().records(event='attempt.started')))
        self.assertEqual(len(list(self.log().records(event='contract.frozen'))), 1)
        self.assertEqual((self.main / frozen['path']).read_bytes(), contents)
        self.assertEqual(self.status()['tasks'], resumed['tasks'])

    def test_resume_timeout_uses_remaining_fast_attempts_without_refreezing(self):
        self.assert_timeout_resume()

    def test_resume_timeout_uses_remaining_standard_attempts_without_refreezing(self):
        self.assert_timeout_resume(standard=True)

    def test_resume_timeout_gate_without_terminal_state_keeps_frozen_contract(self):
        self.docs([plan_task()])
        with mock.patch.object(task, '_feedback', side_effect=SystemExit('stopped after gate')):
            with self.assertRaises(SystemExit):
                self.invoke_plan(run_statuses=('timeout',))
        self.assertEqual(list(self.log().records())[-1]['event'], 'gate.failed')
        self.assertEqual(planrun.task_records(self.saved, self.log())['T1']['state'], 'running')
        self.assertEqual(self.status()['tasks'][0]['state'], 'pending')
        before = self.log().path.read_bytes()
        code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['reason']), (2, 'run_interrupted'), result)
        self.assertEqual(self.log().path.read_bytes(), before)
        self.assertEqual(len(self.requests), 1)
        code, result, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, result['tasks'][0]['state'], result['tasks'][0]['attempts']),
                         (0, 'accepted', 2), result)
        self.assertEqual(len(list(self.log().records(event='contract.frozen'))), 1)
        self.assertEqual([r['data']['attempt'] for r in self.log().records(event='attempt.started')], [1, 2])

    def test_timeout_resumability_requires_current_attempt_contract_and_remaining_budget(self):
        self.docs([plan_task()])
        frozen = self.timeout_ended()
        records = list(self.log().records())
        latest = planrun.task_records(self.saved, self.log())['T1']
        self.assertTrue(planrun.resumable_timeout(self.main, latest))
        for state, reason, attempts in [('accepted', 'accepted', 1), ('exhausted', 'exhausted', 1),
                                        ('blocked', 'dependency_failed', 1), ('rejected', 'outside_allow', 1),
                                        ('rejected', 'timeout', 3)]:
            with self.subTest(state=state, reason=reason, attempts=attempts):
                item = dict(latest, state=state, reason=reason, attempts=attempts)
                self.assertFalse(planrun.resumable_timeout(self.main, item))
        next_attempt = dict(task='T1', event='attempt.started', data=dict(
            attempt=2, run_id='next-run', contract_sha256=frozen['contract_sha256']))
        stale_gate = next(r for r in records if r['event'] == 'gate.failed')
        for suffix in ([next_attempt], [next_attempt, stale_gate],
                       [dict(task='T1', event='contract.frozen', data=frozen), stale_gate]):
            with self.subTest(events=[r['event'] for r in suffix]):
                log = mock.Mock()
                log.records.return_value = iter(records + suffix)
                item = planrun.task_records(self.saved, log)['T1']
                self.assertFalse(planrun.resumable_timeout(self.main, item))

    def test_resume_timeouts_exhausts_frozen_budget_and_stays_terminal(self):
        self.docs([plan_task()])
        self.timeout_ended()
        code, result, _ = self.invoke_plan(resume=True, run_statuses=('timeout', 'timeout'))
        self.assertEqual((code, result['tasks'][0]['state'], result['tasks'][0]['attempts']),
                         (1, 'exhausted', 3), result)
        self.assertEqual([r['data']['attempt'] for r in self.log().records(event='attempt.started')], [1, 2, 3])
        self.assertEqual([r['data']['reason'] for r in self.log().records(event='gate.failed')], ['timeout'] * 3)
        self.assertEqual(len(list(self.log().records(event='contract.frozen'))), 1)
        before = self.log().path.read_bytes()
        self.assertEqual(self.invoke_plan(resume=True)[:2], (1, result))
        self.assertEqual(self.log().path.read_bytes(), before)
        self.assertEqual(len(self.requests), 3)

    def test_resume_non_timeout_rejection_and_dependency_block_are_terminal(self):
        self.docs([plan_task(), plan_task('T2', depends_on=['T1'])])
        self.actions.write_text(json.dumps([{'src/forbidden.py': 'outside\n'}]))
        code, result, _ = self.invoke_plan(run_statuses=('timeout',))
        self.assertEqual((code, [(t['state'], t['reason']) for t in result['tasks']]),
                         (1, [('rejected', 'outside_allow'), ('blocked', 'dependency_failed')]), result)
        before = self.log().path.read_bytes()
        self.assertEqual(self.invoke_plan(resume=True)[:2], (1, result))
        self.assertEqual(self.log().path.read_bytes(), before)
        self.assertEqual(len(self.requests), 1)


    def test_resume_rejects_tampered_contract_without_refreezing_or_dispatch(self):
        self.docs([plan_task()])
        def interrupt(req):
            os.kill(os.getpid(), signal.SIGINT)
        self.assertEqual(self.invoke_plan(before=interrupt)[0], 130)
        frozen = list(self.log().records(event='contract.frozen'))[0]['data']
        path = self.main / frozen['path']
        path.write_text(path.read_text().replace('"attempts": 3', '"attempts": 99'))
        code, result, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, result['tasks'][0]['state'], result['tasks'][0]['reason']),
                         (1, 'rejected', 'contract_tampered'), result)
        self.assertEqual(result['tasks'][0]['attempts'], 1)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(len(list(self.log().records(event='contract.frozen'))), 1)
        before = self.log().path.read_bytes()
        self.assertEqual(self.invoke_plan(resume=True)[0], 1)
        self.assertEqual(self.log().path.read_bytes(), before)

    def test_signal_during_worktree_creation_is_cleaned_before_resume(self):
        self.docs([plan_task()])
        real = worktree.create
        paths = []
        def create(*args, **kwargs):
            wt = real(*args, **kwargs)
            paths.append(wt.path)
            os.kill(os.getpid(), signal.SIGTERM)
            return wt
        with mock.patch.object(worktree, 'create', side_effect=create):
            code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['tasks'][0]['attempts']), (130, 0), result)
        self.assertFalse(Path(paths[0]).exists())
        self.assertEqual(worktree.list_managed(self.main), [])
        self.assertEqual(list(self.log().records(event='attempt.started')), [])
        code, result, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, result['tasks'][0]['attempts']), (0, 1), result)
        self.assertEqual(len(list(self.log().records(event='contract.frozen'))), 1)


    def test_orcexe_symlink_at_repo_root_blocks_plan_run(self):
        import shutil
        self.saved = self.save()
        target = self.directory / 'elsewhere'
        target.mkdir()
        shutil.rmtree(self.main / '.orcexe')
        (self.main / '.orcexe').symlink_to(target, target_is_directory=True)
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result.get('reason')), (2, 'orcexe_invalid'), result)
        self.assertIn('not_seen: orcexe_invalid', error)
        self.assertEqual(self.requests, [])
        self.assertEqual(list(target.iterdir()), [])


    def test_plan_preconditions_dispatch_nothing(self):
        # T59: a task without a plan route is deferred at approval (no_route is no longer a
        # plan-wide precondition); an approved task without one is plan_tampered.
        for reason in ('plan_missing', 'plan_mismatch', 'plan_not_approved', 'plan_stale', 'plan_tampered', 'output_symlink'):
            with self.subTest(reason=reason):
                self.saved = self.save()
                path = self.main / '.orcexe/plan.json'
                value = copy.deepcopy(self.saved)
                identifier = None
                if reason == 'plan_missing':
                    path.unlink()
                elif reason == 'plan_mismatch':
                    identifier = 'plan-other'
                elif reason == 'output_symlink':
                    (self.main / '.orcexe/status.md').symlink_to(self.directory / 'outside')
                else:
                    if reason == 'plan_not_approved':
                        value['status'] = 'proposed'
                    elif reason == 'plan_stale':
                        value['head'] = '0' * 40
                    else:
                        value['tasks'][0]['goal'] = 'tampered'
                    fsio.atomic_write_json(path, value)
                code, result, error = self.invoke_plan(identifier=identifier)
                self.assertEqual((code, result['reason'], error), (2, reason, 'not_seen: ' + reason + '\n'), result)
                self.assertEqual(self.requests, [])
                self.assertEqual(list(self.log().records()), [])
                self.assertIsNone(planrun.chain_tip(self.main, self.saved))
                if reason == 'output_symlink':
                    (self.main / '.orcexe/status.md').unlink()

    def test_status_is_read_only_and_ignores_markdown_and_live_lock(self):
        self.docs([plan_task()])
        code, result, _ = self.invoke_plan()
        self.assertEqual(code, 0, result)
        path = self.main / '.orcexe/status.md'
        path.write_text('AGENT CLAIM: all tasks rejected\n')
        log = self.log().path.read_bytes()
        lock_path = self.main / '.orcexe/do.lock'
        info = do._acquire(lock_path, 'other-run', lambda data: None)
        self.assertIsNotNone(info)
        try:
            with mock.patch.object(task, 'run_task', side_effect=AssertionError('no model')):
                status = self.status()
                self.assertEqual(status['tasks'], result['tasks'])
                self.assertEqual(path.read_text(), 'AGENT CLAIM: all tasks rejected\n')
                self.assertEqual(self.log().path.read_bytes(), log)
            code, refused, _ = self.invoke_plan()
            self.assertEqual((code, refused['reason']), (2, 'task_running'))
        finally:
            planrun._release(lock_path, info)

    def test_docs_have_planrun_reasons_and_commands(self):
        for name in ('README.md', 'plugins/orcexe/skills/orcexe/SKILL.md'):
            text = (ROOT / name).read_text()
            for reason, code in planrun.EXIT_REASONS.items():
                self.assertIn('| `' + reason + '` | ' + str(code) + ' |', text, name)
            for command in ('plan --run', '--resume', 'orcexe status', 'refs/orcexe/plans/'):
                self.assertIn(command, text, name)
