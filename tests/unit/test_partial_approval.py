"""T59: partial plan approval, `deferred`, `--open`, classifier version, tamper twins.

End to end with fake CLIs: a fake Claude manager proposes, `plan --approve` freezes
runnability, `plan --run` dispatches the fake Codex executor through the real gates.
"""

from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
import sys
from unittest import mock

from tests.unit import test_mapping, test_planrun
from tests.unit.test_planning import manager, task as plan_task
from tests.unit.test_questions import project
from tests.unit.test_task import TaskFixture
from orcexe import classify, cli, fsio, ledger, planning, planrun


def doc(identifier, **changes):
    """A fast documentation task the fake executor can finish (README.md)."""
    return plan_task(identifier, **changes)


class PartialApprovalTests(TaskFixture):
    invoke_plan = test_planrun.PlanRunTests.invoke_plan

    def setUp(self):
        super().setUp()
        self.state = self.main / '.orcexe'
        fsio.atomic_write_json(self.state / 'project-map.json', project(self.head))
        self.final = self.directory / 'manager-final'
        wrapper = self.directory / 'fake-claude'
        settings = dict(ORCEXE_FAKE_FINAL_TEXT_FILE=str(self.final),
                        ORCEXE_FAKE_CALLS=str(self.directory / 'manager-calls.jsonl'))
        wrapper.write_text('#!' + sys.executable + '\nimport os,sys,runpy\nos.environ.update(' + repr(settings) + ')\n'
                           'sys.path.insert(0, ' + repr(str(test_mapping.FAKES)) + ')\n'
                           'runpy.run_path(' + repr(str(test_mapping.FAKES / 'claude')) + ', run_name="__main__")\n')
        wrapper.chmod(0o700)
        self.env['ORCEXE_BIN_CLAUDE_CODE'] = str(wrapper)
        self.actions.write_text(json.dumps([{'README.md': 'First accepted\n'}, {'README.md': 'Second accepted\n'},
                                            {'README.md': 'Third accepted\n'}, {'README.md': 'Fourth accepted\n'}]))

    # -- helpers -------------------------------------------------------------------------
    def cli(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cli.main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def plan(self, *options):
        return self.cli('plan', '--repo', str(self.main), '--json', *options)

    def propose(self, tasks):
        self.final.write_text(json.dumps(manager(tasks)))
        code, out, err = self.plan('--goal', 'Fix 1 typo in README.md')
        self.assertEqual((code, err), (0, ''), out)
        self.saved = json.loads(out)
        return self.saved

    def approve(self, *options, expect=0):
        code, out, err = self.plan('--approve', self.saved['plan_id'], *options)
        self.assertEqual(code, expect, (out, err))
        if code == 0:
            self.saved = json.loads(out)
        return code, out, err

    def c33(self):
        """4 tasks: T2 risky (unrunnable), T3 depends on T2, T1/T4 independent."""
        self.propose([doc('T1'), doc('T2', lane='risky'), doc('T3', depends_on=['T2']), doc('T4')])
        return self.approve()

    def files(self):
        names = ('plan.json', 'plan.md', 'decisions.jsonl', 'ledger.jsonl', 'status.md')
        snapshot = {n: (self.state / n).read_bytes() if (self.state / n).exists() else None for n in names}
        snapshot['head-ref'] = self.git_run(self.main, 'for-each-ref', 'refs/orcexe/')
        return snapshot

    def decisions(self):
        # TaskFixture.setUp approved a fixture plan first; only this plan's decisions count.
        return [d for d in fsio.read_jsonl(self.state / 'decisions.jsonl') if d['plan_id'] == self.saved['plan_id']]

    def anchors(self):
        return [r['data'] for r in self.log().records(event='decision.recorded')
                if r['data']['plan_id'] == self.saved['plan_id']]

    def revocations(self):
        return [r['data'] for r in self.log().records(event='approval.revoked')]

    def write_plan(self, value):
        """Rewrite the published plan bytes in place (an edit outside orcexe)."""
        path = self.state / 'plan.json'
        target = path.resolve()
        target.write_text(json.dumps(value))

    # -- C33 end to end ----------------------------------------------------------------
    def test_c33_partial_approval_runs_independent_tasks_and_defers_the_rest(self):
        proposed = copy.deepcopy(self.propose([doc('T1'), doc('T2', lane='risky'),
                                               doc('T3', depends_on=['T2']), doc('T4')]))
        self.assertIn('## Will be deferred\n\n- T2 (risky): lane_not_supported', (self.state / 'plan.md').read_text())
        self.approve()
        approval = self.saved['approval']
        self.assertEqual((self.saved['plan_id'], self.saved['content_sha256']),
                         (proposed['plan_id'], proposed['content_sha256']))
        self.assertEqual(approval['approved'], ['T1', 'T4'])
        self.assertEqual([(d['id'], d['reason'], d['cause']) for d in approval['deferred']],
                         [('T2', 'lane_not_supported', None), ('T3', 'dependency_deferred', 'T2')])
        self.assertEqual(approval['opened_from'], None)
        self.assertEqual(approval['approval_sha256'], planning.approval_digest(self.saved, approval))
        record = self.decisions()[-1]
        self.assertEqual((record['kind'], record['decision_id'], record['approval_sha256']),
                         ('plan_approval', approval['decision_id'], approval['approval_sha256']))
        self.assertEqual(self.anchors(), [dict(kind='plan_approval', decision_id=approval['decision_id'],
                                               plan_id=self.saved['plan_id'],
                                               record_sha256=planning.record_digest(record))])
        rows = [(r['task'], r['data']['from'], r['data']['to'], r['data']['reason'])
                for r in self.log().records(event='task.state')]
        self.assertEqual(rows, [('T2', 'pending', 'deferred', 'lane_not_supported'),
                                ('T3', 'pending', 'deferred', 'dependency_deferred')])
        markdown = (self.state / 'plan.md').read_text()
        self.assertIn('Status: approved (2 of 4; 2 deferred)', markdown)
        # §2.6 layout after the decision: approved ids with lanes, deferred ids with reason and cause.
        self.assertIn('\n## Will run after approval\n\nT1 (fast) · T4 (fast)\n\n## Will be deferred\n\n'
                      '- T2 (risky): lane_not_supported — lane: risky (this version runs fast and standard)\n'
                      '- T3: dependency_deferred (depends on T2)\n', markdown)
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result['state']), (1, 'partial'), (result, error))
        states = {t['task_id']: (t['state'], t['reason'], t['attempts']) for t in result['tasks']}
        self.assertEqual(states, {'T1': ('accepted', 'accepted', 1), 'T2': ('deferred', 'lane_not_supported', 0),
                                 'T3': ('deferred', 'dependency_deferred', 0), 'T4': ('accepted', 'accepted', 1)})
        self.assertEqual([r.task for r in self.requests], ['T1', 'T4'])
        # The land form warns that landing closes the deferred tasks for good (decision 027 Ş4).
        self.assertEqual(result['land']['deferred'], ['T2', 'T3'])
        self.assertIn('Warning: landing closes deferred T2/T3', (self.state / 'status.md').read_text())
        code, out, _ = self.cli('plan', '--run', self.saved['plan_id'], '--repo', str(self.main))
        self.assertIn('Warning: landing closes deferred T2/T3', out)
        # --resume never dispatches a deferred task either; the finished run is unchanged.
        before = self.log().path.read_bytes()
        code, again, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, again['tasks']), (1, result['tasks']))
        self.assertEqual(self.log().path.read_bytes(), before)
        self.assertEqual([r.task for r in self.requests], ['T1', 'T4'])

    # -- approval rules ------------------------------------------------------------------
    def test_defer_by_human_takes_dependants_and_nothing_runnable_writes_nothing(self):
        self.propose([doc('T1'), doc('T2', depends_on=['T1']), doc('T3')])
        before = self.files()
        code, out, err = self.approve('--defer', 'T9', expect=2)
        self.assertEqual(err, 'not_seen: plan_rejected\nrefused: T9 task_unknown (not in this plan)\n')
        self.assertEqual(self.files(), before)
        code, out, err = self.approve('--defer', 'T1,T3', expect=2)
        self.assertEqual(err, 'not_seen: plan_rejected\n'
                              'deferred: T1 deferred_by_human (human: --defer)\n'
                              'deferred: T2 dependency_deferred (depends on T1)\n'
                              'deferred: T3 deferred_by_human (human: --defer)\n')
        self.assertEqual(self.files(), before)
        self.approve('--defer', 'T1')
        self.assertEqual(self.saved['approval']['approved'], ['T3'])
        self.assertEqual([(d['id'], d['reason'], d['cause']) for d in self.saved['approval']['deferred']],
                         [('T1', 'deferred_by_human', None), ('T2', 'dependency_deferred', 'T1')])

    def test_flag_usage_is_checked_by_the_parser(self):
        for options in (['--goal', 'x', '--defer', 'T1'], ['--approve', 'p', '--defer', 'T1', '--open', 'T2'],
                        ['--approve', 'p', '--defer', 'X1'], ['--approve', 'p', '--open', 'T1,T1']):
            with self.subTest(options=options):
                with self.assertRaises(SystemExit) as raised:
                    self.plan(*options)
                self.assertEqual(raised.exception.code, 2)

    def test_deferred_task_is_never_dispatched_by_task(self):
        self.c33()
        for key, reason in (('T2', 'task_deferred'), ('T3', 'task_deferred')):
            code, result, error = self.invoke(task_id=key)
            self.assertEqual((code, result['reason'], result['attempts']), (2, reason, 0), result)
            self.assertEqual(error, 'not_seen: task_deferred\n')
        self.assertFalse(self.counter.exists())

    def test_approval_and_open_hold_the_dispatch_lock(self):
        self.propose([doc('T1')])
        lock = self.state / 'do.lock'
        lock.write_text(json.dumps(dict(pid=os.getpid(), task_id='other')))
        before = self.files()
        code, out, err = self.approve(expect=2)
        self.assertEqual(err, 'not_seen: task_running\n')
        self.assertEqual(self.files(), before)
        lock.unlink()

    # -- open ----------------------------------------------------------------------------
    def test_open_is_a_chained_anchored_decision_and_refuses_unrunnable(self):
        self.propose([doc('T1'), doc('T2', lane='risky'), doc('T3'), doc('T4', depends_on=['T3'])])
        self.approve('--defer', 'T3')
        first = self.saved['approval']
        self.assertEqual([d['id'] for d in first['deferred']], ['T2', 'T3', 'T4'])
        before = self.files()
        for ids, line in (('T2', 'refused: T2 lane_not_supported'), ('T4', 'refused: T4 dependency_deferred (depends on T3)'),
                          ('T1', 'refused: T1 not_deferred'), ('T7', 'refused: T7 task_unknown')):
            with self.subTest(ids=ids):
                code, out, err = self.approve('--open', ids, expect=2)
                self.assertTrue(err.startswith('not_seen: plan_rejected\n' + line), err)
                self.assertEqual(self.files(), before)
        # Opening a task with its deferred dependency in the same call works.
        self.approve('--open', 'T4,T3')
        second = self.saved['approval']
        self.assertEqual((second['approved'], [d['id'] for d in second['deferred']]), (['T1', 'T3', 'T4'], ['T2']))
        self.assertEqual(second['opened_from'], first['decision_id'])
        self.assertEqual(self.saved['plan_id'], before and json.loads(before['plan.json'])['plan_id'])
        kinds = [(d['kind'], d['decision_id'], d['opened_from']) for d in self.decisions()]
        self.assertEqual(kinds, [('plan_approval', first['decision_id'], None),
                                 ('plan_open', second['decision_id'], first['decision_id'])])
        self.assertEqual(self.decisions()[-1]['opened'], ['T4', 'T3'])
        self.assertEqual([a['kind'] for a in self.anchors()], ['plan_approval', 'plan_open'])
        opened = [(r['task'], r['data']['from'], r['data']['to']) for r in self.log().records(event='task.state')
                  if r['data']['from'] == 'deferred']
        self.assertEqual(opened, [('T3', 'deferred', 'pending'), ('T4', 'deferred', 'pending')])
        code, result, error = self.invoke_plan()
        self.assertEqual({t['task_id']: t['state'] for t in result['tasks']},
                         {'T1': 'accepted', 'T2': 'deferred', 'T3': 'accepted', 'T4': 'accepted'}, error)

    def test_open_re_reasons_a_dependant_that_stays_deferred(self):
        self.propose([doc('T1'), doc('T2'), doc('T3', depends_on=['T2'])])
        self.approve('--defer', 'T2')
        self.approve('--open', 'T2')
        self.assertEqual(self.saved['approval']['approved'], ['T1', 'T2'])
        # P3-4: T3 was never deferred by the human; it keeps dependency_deferred with its cause.
        self.assertEqual([(d['id'], d['reason'], d['cause'], d['evidence']) for d in self.saved['approval']['deferred']],
                         [('T3', 'dependency_deferred', 'T2', 'depends on T2; not opened with it')])
        self.approve('--open', 'T3')
        self.assertEqual((self.saved['approval']['approved'], self.saved['approval']['deferred']),
                         (['T1', 'T2', 'T3'], []))
        self.assertEqual(len(self.decisions()), 3)

    def test_open_needs_an_approved_plan_and_is_stale_after_landing(self):
        self.propose([doc('T1'), doc('T2')])
        code, out, err = self.approve('--open', 'T2', expect=2)
        self.assertEqual(err, 'not_seen: plan_not_approved\n')
        self.approve('--defer', 'T2')
        code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['land']['deferred']), (1, ['T2']))
        self.git_run(self.main, 'merge', '--ff-only', '-q', result['land']['ref'])
        before = self.files()
        code, out, err = self.approve('--open', 'T2', expect=2)
        self.assertEqual(err, 'not_seen: plan_stale\n')
        self.assertEqual(self.files(), before)

    # -- classifier version and pre-T59 plans ---------------------------------------------
    def test_classifier_change_is_stale_everywhere(self):
        self.propose([doc('T1'), doc('T2')])
        with mock.patch.object(classify, 'CLASSIFIER_VERSION', 'lane.v3'):
            code, out, err = self.approve(expect=2)
        self.assertEqual(err, 'not_seen: plan_stale\ndetail: classifier_changed\n')
        self.approve('--defer', 'T2')
        before = self.files()
        with mock.patch.object(classify, 'CLASSIFIER_VERSION', 'lane.v3'):
            code, out, err = self.approve('--open', 'T2', expect=2)
            self.assertEqual(err, 'not_seen: plan_stale\ndetail: classifier_changed\n')
            code, result, error = self.invoke_plan()
            self.assertEqual((code, result['reason'], error), (2, 'plan_stale', 'not_seen: plan_stale\ndetail: classifier_changed\n'))
            code, result, error = self.invoke()
            self.assertEqual((code, result['reason'], error), (2, 'plan_stale', 'not_seen: plan_stale\ndetail: classifier_changed\n'))
        self.assertEqual(self.files(), before)
        self.assertFalse(self.counter.exists())

    def test_plan_approved_before_t59_is_stale_never_fully_approved(self):
        self.c33()
        legacy = copy.deepcopy(self.saved)
        legacy.pop('approval')  # excluded from content_sha256: id and hash still verify
        self.write_plan(legacy)
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result['reason'], error), (2, 'plan_stale', 'not_seen: plan_stale\n'))
        code, result, error = self.invoke(task_id='T1')
        self.assertEqual((code, result['reason']), (2, 'plan_stale'))
        self.assertFalse(self.counter.exists())

    # -- tamper twins (T57 §2.5 / §4) ---------------------------------------------------------
    def assert_refused(self, reason, detail=None):
        for runner in ('plan', 'task'):
            if runner == 'plan':
                code, result, error = self.invoke_plan()
                expected_code = 2
            else:
                code, result, error = self.invoke(task_id='T1')
                expected_code = 2 if reason != 'state_tampered' else 1
            self.assertEqual((code, result['reason']), (expected_code, reason), (runner, result, error))
            if detail == 'approval_revoked':
                self.assertIn('detail: approval_revoked\nRe-approve', error)
            elif detail:
                self.assertIn('detail: ' + detail, error)
                self.assertIn('approval.revoked', error)  # the unanchored-decision recovery hint
                self.assertEqual(self.revocations(), [dict(plan_id=self.saved['plan_id'], reason=detail)])
                # §4: the first refusal revoked the approval; every later dispatch asks to re-approve.
                reason, detail = 'plan_not_approved', 'approval_revoked'
        self.assertFalse(self.counter.exists())
        self.assertEqual(self.requests, [])

    def test_twin_1_moving_a_deferred_task_to_approved(self):
        self.c33()
        forged = copy.deepcopy(self.saved)
        forged['approval']['approved'].append('T2')
        forged['approval']['deferred'] = [d for d in forged['approval']['deferred'] if d['id'] != 'T2']
        self.write_plan(forged)
        self.assert_refused('plan_tampered')

    def test_twin_1b_consistent_forgery_still_fails_recomputed_reasons(self):
        # Lists, hash, decision record AND its ledger anchor all agree; the plan-derived
        # reason (T2 risky -> lane_not_supported) is recomputed and refuses it.
        self.c33()
        forged = copy.deepcopy(self.saved)
        approval = forged['approval']
        approval['approved'] = ['T1', 'T2', 'T4']
        approval['deferred'] = [dict(id='T3', reason='deferred_by_human', cause=None, evidence='human: --defer')]
        approval['approval_sha256'] = planning.approval_digest(forged, approval)
        record = planning._record(forged, 'plan_approval', approval, [], approval['at'])
        self.write_plan(forged)
        fsio.atomic_write_text(self.state / 'decisions.jsonl', json.dumps(record) + '\n')
        self.log().append('decision.recorded', 'plan', dict(kind='plan_approval', decision_id=approval['decision_id'],
                          plan_id=forged['plan_id'], record_sha256=planning.record_digest(record)))
        self.assert_refused('plan_tampered')

    def test_twin_2_deleting_the_decision_record(self):
        self.c33()
        (self.state / 'decisions.jsonl').write_text('')
        self.assert_refused('plan_tampered')

    def test_twin_3_hand_computed_approval_hash(self):
        self.c33()
        forged = copy.deepcopy(self.saved)
        approval = forged['approval']
        approval['deferred'] = [dict(id='T2', reason='lane_not_supported', cause=None,
                                     evidence=approval['deferred'][0]['evidence'])]
        approval['approved'] = ['T1', 'T3', 'T4']
        approval['approval_sha256'] = planning.approval_digest(forged, approval)
        self.write_plan(forged)
        self.assert_refused('plan_tampered')

    def test_twin_4_recomputed_hash_and_plan_open_line_without_ledger_anchor(self):
        self.propose([doc('T1'), doc('T2')])
        self.approve('--defer', 'T2')
        forged = copy.deepcopy(self.saved)
        approval = forged['approval']
        approval.update(approved=['T1', 'T2'], deferred=[], opened_from=approval['decision_id'],
                        decision_id='d-0000000000000001')
        approval['approval_sha256'] = planning.approval_digest(forged, approval)
        record = planning._record(forged, 'plan_open', approval, ['T2'], approval['at'])
        self.write_plan(forged)
        fsio.append_jsonl(self.state / 'decisions.jsonl', record)
        self.assert_refused('state_tampered', 'decision_unanchored')
        self.assertFalse((self.state / 'state-tampered').exists())  # no marker: propose again

    def test_twin_4b_opened_ledger_row_without_plan_open_decision(self):
        self.propose([doc('T1'), doc('T2')])
        self.approve('--defer', 'T2')
        self.log().append('task.state', 'T2', {'plan_id': self.saved['plan_id'], 'from': 'deferred',
                                               'to': 'pending', 'reason': 'opened'})
        self.assert_refused('state_tampered', 'open_unanchored')

    def test_twin_4c_edited_decision_record_breaks_its_anchor(self):
        self.c33()
        records = self.decisions()
        records[-1]['answer'] = 'Approved by someone else'
        (self.state / 'decisions.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
        self.assert_refused('state_tampered', 'decision_unanchored')

    def test_revoked_approval_needs_a_fresh_approval_on_a_verified_ledger(self):
        self.propose([doc('T1'), doc('T2')])
        self.approve('--defer', 'T2')
        genuine = copy.deepcopy(self.saved)
        forged = copy.deepcopy(self.saved)
        forged['approval']['approved'] = ['T1', 'T2']
        forged['approval']['deferred'] = []
        forged['approval']['opened_from'] = forged['approval']['decision_id']
        forged['approval']['decision_id'] = 'd-00000000000000ff'
        forged['approval']['approval_sha256'] = planning.approval_digest(forged, forged['approval'])
        self.write_plan(forged)
        fsio.append_jsonl(self.state / 'decisions.jsonl',
                          planning._record(forged, 'plan_open', forged['approval'], ['T2'], forged['approval']['at']))
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result['reason']), (2, 'state_tampered'), error)
        self.assertEqual(self.revocations(), [dict(plan_id=self.saved['plan_id'], reason='decision_unanchored')])
        revoked = 'not_seen: plan_not_approved\ndetail: approval_revoked\n'
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result['reason']), (2, 'plan_not_approved'))
        self.assertTrue(error.startswith(revoked) and 'Re-approve' in error, error)
        code, result, error = self.invoke(task_id='T1')
        self.assertEqual((code, result['reason']), (2, 'plan_not_approved'))
        self.assertTrue(error.startswith(revoked), error)
        code, out, error = self.approve('--open', 'T2', expect=2)
        self.assertEqual(error, revoked)
        self.assertFalse(self.counter.exists())
        # A fresh approval only on a verified ledger: a corrupt tail refuses and writes nothing.
        path = self.log().path
        good = path.read_bytes()
        path.write_bytes(good + b'forged\n')
        before = self.files()
        code, out, error = self.approve(expect=2)
        self.assertEqual(error.splitlines()[0], 'not_seen: ledger_corrupt')
        self.assertEqual(self.files(), before)
        path.write_bytes(good)
        # P2-5: a bare re-approval would drop the human's --defer T2; it must be restated.
        before = self.files()
        code, out, error = self.approve(expect=2)
        self.assertEqual(error, 'not_seen: plan_rejected\n'
                                'refused: T2 deferred_by_human (restate with --defer T2 or --defer none)\n')
        self.assertEqual(self.files(), before)
        self.approve('--defer', 'none')
        self.assertEqual(self.saved['approval']['approved'], ['T1', 'T2'])
        self.assertEqual(self.saved['approval']['opened_from'], None)
        self.assertNotEqual(self.saved['approval']['decision_id'], genuine['approval']['decision_id'])
        # The forged opened-chain before the fresh approval no longer counts; the plan runs.
        code, result, error = self.invoke_plan()
        self.assertEqual({t['task_id']: t['state'] for t in result['tasks']},
                         {'T1': 'accepted', 'T2': 'accepted'}, error)
        # A non-revoked approved plan still cannot be approved twice.
        code, out, error = self.approve(expect=2)
        self.assertEqual(error, 'not_seen: plan_not_proposed\n')

    def lines(self):
        return [json.loads(l) for l in (self.state / 'decisions.jsonl').read_text().splitlines() if l.strip()]

    def test_twin_6a_older_wider_approval_replayed_after_reproposal(self):
        # L2 P1-1 (a): same plan_id re-proposed and approved narrower; plan.json and a copy of the
        # older, wider (genuinely anchored) decision line are put back. No ledger write.
        tasks = [doc('T1'), doc('T2')]
        first = self.propose(tasks)
        self.approve()
        wide_plan = (self.state / 'plan.json').resolve().read_text()
        wide = [l for l in self.lines() if l.get('plan_id') == first['plan_id']][-1]
        second = self.propose(tasks)
        self.assertEqual((second['plan_id'], second['status']), (first['plan_id'], 'proposed'))
        self.approve('--defer', 'T2')
        (self.state / 'plan.json').resolve().write_text(wide_plan)
        (self.state / 'decisions.jsonl').write_text(''.join(json.dumps(l) + '\n' for l in self.lines() + [wide]))
        self.saved = json.loads(wide_plan)
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result['reason']), (2, 'state_tampered'), result)
        self.assertIn('detail: decision_superseded', error)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.counter.exists())
        self.assertEqual(self.revocations(), [dict(plan_id=first['plan_id'], reason='decision_superseded')])

    def test_twin_6b_replaced_plan_restored_by_one_file(self):
        # L2 P1-1 (b): a different proposal replaced P1; only plan.json is put back.
        first = self.propose([doc('T1'), doc('T2')])
        self.approve()
        old = (self.state / 'plan.json').resolve().read_text()
        second = self.propose([doc('T1'), doc('T2'), doc('T3')])
        self.approve('--defer', 'T2')
        self.assertNotEqual(first['plan_id'], second['plan_id'])
        decisions = (self.state / 'decisions.jsonl').read_bytes()
        (self.state / 'plan.json').resolve().write_text(old)
        self.saved = json.loads(old)
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result['reason']), (2, 'state_tampered'), result)
        self.assertIn('detail: decision_superseded', error)
        self.assertEqual(self.requests, [])
        self.assertEqual((self.state / 'decisions.jsonl').read_bytes(), decisions)
        code, result, error = self.invoke(task_id='T1')
        self.assertEqual((code, result['reason']), (2, 'plan_not_approved'))
        # P3-2: neither status nor plan.md calls the revoked plan approved.
        code, out, _ = self.cli('status', '--repo', str(self.main), '--json')
        self.assertEqual(json.loads(out)['plan_status'], 'revoked')
        self.assertIn('Status: revoked', (self.state / 'plan.md').read_text())

    def test_reapproval_after_revocation_recomputes_from_content_even_if_plan_file_is_forged(self):
        # L2 P3-2 (a): the forged approval stays in plan.json; re-approval ignores it.
        self.propose([doc('T1'), doc('T2')])
        self.approve('--defer', 'T2')
        forged = copy.deepcopy(self.saved)
        forged['approval']['approved'] = ['T1', 'T2']
        forged['approval']['deferred'] = []
        forged['approval']['approval_sha256'] = planning.approval_digest(forged, forged['approval'])
        self.write_plan(forged)
        code, result, error = self.invoke_plan()
        self.assertEqual((code, result['reason']), (2, 'plan_tampered'))
        self.assertEqual(self.revocations(), [])  # plan shape tamper: no anchor question, no revocation
        records = self.lines()
        records[-1]['answer'] = 'edited'
        (self.state / 'decisions.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in records))
        self.write_plan(self.saved)
        code, result, error = self.invoke_plan()
        self.assertIn('detail: decision_unanchored', error)
        self.write_plan(dict(forged, status='approved'))
        self.approve('--defer', 'T2')
        self.assertEqual((self.saved['approval']['approved'], self.saved['status']), (['T1'], 'approved'))
        self.assertIn('Status: approved (1 of 2; 1 deferred)', (self.state / 'plan.md').read_text())

    def test_plan_md_suggests_open_only_for_openable_tasks(self):
        # L2 P3-3: T2 (risky) and its dependant T3 can never be opened in this version.
        self.propose([doc('T1'), doc('T2', lane='risky'), doc('T3', depends_on=['T2']), doc('T4')])
        self.assertNotIn('--open T2', (self.state / 'plan.md').read_text())
        self.approve('--defer', 'T4')
        markdown = (self.state / 'plan.md').read_text()
        self.assertNotIn('--open T2', markdown)
        self.assertIn('--open T4`', markdown)
        self.assertIn("Cannot be opened in this version (lane or the plan's recorded route): T2, T3", markdown)

    def test_supported_lanes_change_is_stale_not_tampered(self):
        # L2 P3-5: the runnable lanes are part of the decision.
        self.c33()
        with mock.patch.object(planning, 'SUPPORTED_LANES', ('fast', 'standard', 'risky')):
            code, result, error = self.invoke_plan()
            self.assertEqual((code, result['reason']), (2, 'plan_stale'))
            self.assertIn('detail: lanes_changed', error)
            code, out, error = self.approve('--open', 'T2', expect=2)
            self.assertEqual(error, 'not_seen: plan_stale\ndetail: lanes_changed\n')
        self.assertEqual(self.requests, [])

    def test_ledger_repair_line_names_the_decision(self):
        # L2 P3-6.
        self.c33()
        record = list(self.log().records(event='decision.recorded'))[-1]
        line = ledger.describe(record)
        self.assertIn('kind=plan_approval', line)
        self.assertIn('decision_id=' + self.saved['approval']['decision_id'], line)

    def test_decision_forged_during_an_executor_window_stops_that_run(self):
        # L2 P3-1: checked again right after the task; later tasks are not dispatched.
        self.propose([doc('T1'), doc('T2'), doc('T3')])
        self.approve('--defer', 'T3')

        def forge(request):
            if request.task != 'T1':
                return
            plan = json.loads((self.state / 'plan.json').resolve().read_text())
            approval = plan['approval']
            approval.update(approved=['T1', 'T2', 'T3'], deferred=[], opened_from=approval['decision_id'],
                            decision_id='d-00000000000000bb')
            approval['approval_sha256'] = planning.approval_digest(plan, approval)
            (self.state / 'plan.json').resolve().write_text(json.dumps(plan))
            fsio.append_jsonl(self.state / 'decisions.jsonl',
                              planning._record(plan, 'plan_open', approval, ['T3'], approval['at']))
        code, result, error = self.invoke_plan(before=forge)
        self.assertEqual(code, 2, (result, error))
        self.assertIn(result['reason'], ('state_tampered',))
        self.assertEqual([r.task for r in self.requests], ['T1'])
        self.assertEqual([r['reason'] for r in self.revocations()], ['decision_unanchored'])

    def test_twin_5_deleting_the_approval_field(self):
        self.c33()
        forged = copy.deepcopy(self.saved)
        del forged['approval']
        self.write_plan(forged)
        self.assert_refused('plan_stale')

    def test_open_refuses_tampered_plan_and_writes_nothing(self):
        self.propose([doc('T1'), doc('T2')])
        self.approve('--defer', 'T2')
        forged = copy.deepcopy(self.saved)
        forged['approval']['approved'] = ['T1', 'T2']
        forged['approval']['deferred'] = []
        self.write_plan(forged)
        before = self.files()
        code, out, err = self.approve('--open', 'T2', expect=2)
        self.assertEqual(err, 'not_seen: plan_tampered\n')
        self.assertEqual(self.files(), before)


class DeferralUnitTests(TaskFixture):
    def test_deferral_is_a_fixed_point_of_its_own_held_set(self):
        tasks = [plan_task('T1', route={'tool': None, 'reason': 'no_eligible_candidate'}),
                 plan_task('T2', depends_on=['T1']), plan_task('T3', lane='risky'),
                 plan_task('T4'), plan_task('T5', depends_on=['T4', 'T3'])]
        for item in tasks:
            item.setdefault('route', dict(tool='codex', model='m', effort='high'))
        result = planning.deferral(tasks, {'T4'})
        self.assertEqual([(d['id'], d['reason'], d['cause'], d['evidence']) for d in result], [
            ('T1', 'no_route', None, 'route: none (no_eligible_candidate)'),
            ('T2', 'dependency_deferred', 'T1', 'depends on T1'),
            ('T3', 'lane_not_supported', None, 'lane: risky (this version runs fast and standard)'),
            ('T4', 'deferred_by_human', None, 'human: --defer'),
            ('T5', 'dependency_deferred', 'T4', 'depends on T4')])
        self.assertTrue({d['reason'] for d in result} <= set(planning.DEFER_REASONS))
        held = {d['id'] for d in result if d['reason'] in ('deferred_by_human', 'dependency_deferred')}
        self.assertEqual(planning.deferral(tasks, held), result)

    def test_deferred_rows_are_overlaid_from_the_decision_not_the_ledger(self):
        value = self.save([plan_task('T1'), plan_task('T2')], held={'T2'})
        records = planrun.task_records(value, ledger.Ledger(ledger.default_path(self.main)))
        self.assertEqual((records['T1']['state'], records['T2']['state'], records['T2']['reason']),
                         ('pending', 'deferred', 'deferred_by_human'))
