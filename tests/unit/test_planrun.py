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
from orcexe import cli, do, fsio, isolation, planrun, proc, task, worktree
from orcexe.adapters import CodexAdapter


class PlanRunTests(TaskFixture):
    def invoke_plan(self, resume=False, before=None, accepted=True, identifier=None):
        def factory():
            adapter = CodexAdapter(env=self.env, kill_grace_s=.1)
            real = adapter.run
            def run(request):
                self.requests.append(request)
                self.briefs.append(Path(request.brief_path).read_text())
                if before:
                    before(request)
                return real(request)
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
        self.assertEqual(read.call_count, 1)
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
            'T4': ('accepted', 'accepted', 1), 'T5': ('blocked', 'lane_not_supported', 0),
            'T6': ('blocked', 'dependency_failed', 0)})
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


    def test_plan_preconditions_dispatch_nothing(self):
        for reason in ('plan_missing', 'plan_mismatch', 'plan_not_approved', 'plan_stale', 'plan_tampered', 'no_route', 'output_symlink'):
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
                elif reason == 'no_route':
                    self.saved = self.save([plan_task(), plan_task('T2', route={'tool': None})])
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
