"""T61: `plan --run --max-calls N --max-minutes M`, counted by the orchestrator around each call."""

import functools
import json
import unittest
from unittest import mock

from tests.unit.test_risky_lane import RiskyFixture
from tests.unit.test_task import APP, TEST
from tests.unit.test_task_l1 import rubric
from tests.unit.test_planning import task as plan_task
from orcexe import planrun, task
from orcexe.adapters.codex import CodexAdapter


CODEX = dict(tool='codex', model='gpt-6.1-sol', effort='high')


class BudgetUnitTests(unittest.TestCase):
    def test_counts_each_started_call_and_refuses_the_one_that_would_exceed(self):
        now = [0.0]
        budget = planrun.Budget(2, None, clock=lambda: now[0])
        budget.take()
        budget.take()
        self.assertEqual((budget.calls, budget.refusal()), (2, 'max_calls'))
        with self.assertRaises(planrun.BudgetStop) as stop:
            budget.take()
        self.assertEqual((stop.exception.reason, budget.calls, budget.reason), ('max_calls', 2, 'max_calls'))

    def test_wall_clock_is_checked_at_every_call_start(self):
        now = [0.0]
        budget = planrun.Budget(None, 1.5, clock=lambda: now[0])
        budget.take()
        now[0] = 90.0  # exactly the limit: not exceeded
        budget.take()
        now[0] = 90.5
        with self.assertRaises(planrun.BudgetStop):
            budget.take()
        self.assertEqual(budget.event('plan-x'), dict(plan_id='plan-x', max_calls=None, calls=2, max_minutes='1.5',
                                                      elapsed_ms=90500, reason='max_minutes'))


class PlanBudgetTests(RiskyFixture):
    def setUp(self):
        super().setUp()
        self.use_plan('standard')

    def use_plan(self, lane, count=1):
        if lane == 'fast':
            tasks = [plan_task('T' + str(i), route=dict(CODEX)) for i in range(1, count + 1)]
        else:
            tasks = [plan_task(goal='Return two and update its test', lane=lane,
                               files={'allow': ['src/*.py', 'tests/**']}, done=['app returns 2 and its test passes'],
                               route=dict(CODEX))]
        self.saved = self.save(tasks)
        self.plan_id = self.saved['plan_id']

    def plan_run(self, *extra, json_output=True):
        code, out, err = self.run_cli('plan', '--run', self.plan_id, '--repo', str(self.main),
                                      *((['--json'] if json_output else []) + list(extra)))
        return code, json.loads(out) if json_output else out, err

    def budget_events(self):
        return [r['data'] for r in self.log().records(event='plan.budget')]

    def started(self):
        return {event: self.events().count(event) for event in ('attempt.started', 'l1.started', 'audit.started')}

    def test_stop_then_resume_with_a_new_budget_continues_without_refund(self):
        self.evaluator([rubric(False), rubric()])
        # Attempt 2 writes a different tree (a rejected tree's L1 verdict is remembered, never re-asked).
        two, test = APP.replace('1', '2'), TEST.replace('1', '2')
        self.actions.write_text(json.dumps([{'src/app.py': two, 'tests/test_app.py': test},
                                            {'src/app.py': two + '# validated\n', 'tests/test_app.py': test}]))
        code, result, error = self.plan_run('--max-calls', '2')
        # Call 1 executor, call 2 L1 (rejects): attempt 2 would be call 3, so it never starts.
        self.assertEqual((code, result['state'], result['stop_reason'], result['calls']), (1, 'partial', 'budget', 2),
                         (result, error))
        item = result['tasks'][0]
        self.assertEqual((item['state'], item['reason'], item['attempts']), ('interrupted', 'budget', 1))
        self.assertEqual(json.loads(self.counter.read_text()), 1)
        self.assertEqual(self.accepted_refs(), '')
        self.assertIsNone(result['land'])
        self.assertIn(planrun.chain_tip(self.main, self.saved), (None, self.head))
        event, = self.budget_events()
        self.assertEqual({k: event[k] for k in ('plan_id', 'max_calls', 'calls', 'max_minutes', 'reason')},
                         dict(plan_id=self.plan_id, max_calls=2, calls=2, max_minutes=None, reason='max_calls'))
        self.assertEqual(type(event['elapsed_ms']), int)
        # Not finished: a plain re-run refuses; the resumed run gets a new budget.
        code, again, error = self.plan_run()
        self.assertEqual((code, again['reason']), (2, 'run_interrupted'), (again, error))
        code, resumed, error = self.plan_run('--resume', '--max-calls', '5')
        item = resumed['tasks'][0]
        self.assertEqual((code, resumed['state'], resumed['calls']), (0 if self.accepted_state == 'accepted' else 1,
                         'completed' if self.accepted_state == 'accepted' else 'partial', 2), (resumed, error))
        self.assertNotIn('stop_reason', resumed)
        # Attempt numbers continue: the spent attempt is not refunded.
        self.assertEqual((item['state'], item['attempts']), (self.accepted_state, 2))
        attempts = [r['data']['attempt'] for r in self.log().records(event='attempt.started')]
        self.assertEqual(attempts, [1, 2])
        self.assertEqual(len(self.budget_events()), 1)

    def test_failed_unavailable_and_fallback_calls_are_counted(self):
        # The executor's first call fails (quota); every L1 candidate is unavailable (quota).
        self.executor_scenarios(['quota', 'success'])
        self.evaluator([rubric()], scenario='quota')
        code, result, error = self.plan_run('--max-calls', '50')
        started = self.started()
        self.assertGreaterEqual(started['l1.started'], 1, (result, error))
        self.assertEqual(result['calls'], started['attempt.started'] + started['l1.started'], (result, started))
        self.assertEqual(result['tasks'][0]['reason'], 'l1_unavailable', result)
        # The same failed calls exhaust a budget sized below them.
        self.setUp()
        self.executor_scenarios(['quota', 'success'])
        code, result, error = self.plan_run('--max-calls', '1')
        item = result['tasks'][0]
        self.assertEqual((result['calls'], item['state'], item['reason'], item['attempts']),
                         (1, 'interrupted', 'budget', 1), (result, error))
        self.assertEqual(len(list(self.log().records(event='attempt.ended'))), 1)

    def test_generous_budget_completes_like_no_budget(self):
        self.evaluator([rubric()])
        code, result, error = self.plan_run('--max-calls', '50', '--max-minutes', '60')
        self.assertEqual((result['tasks'][0]['state'], result['calls']), (self.accepted_state, 2), (result, error))
        self.assertNotIn('stop_reason', result)
        self.assertEqual(self.budget_events(), [])

    def test_without_flags_nothing_is_counted_or_reported(self):
        self.evaluator([rubric()])
        code, result, error = self.plan_run()
        self.assertEqual(result['tasks'][0]['state'], self.accepted_state, (result, error))
        self.assertFalse({'calls', 'stop_reason'} & set(result))
        self.assertEqual(self.budget_events(), [])

    def test_wall_clock_stop_starts_no_new_call(self):
        self.evaluator([rubric()])
        now = [1000.0]

        def factory():
            adapter = CodexAdapter(env=self.env, kill_grace_s=.1)
            real = adapter.run

            def run(request):
                result = real(request)
                now[0] += 180  # the executor call took three minutes
                return result
            adapter.run = run
            return adapter
        original = planrun.run_plan
        with mock.patch.object(planrun, 'run_plan', functools.partial(original, _clock=lambda: now[0])), \
                mock.patch.object(task, 'CodexAdapter', side_effect=factory):
            code, result, error = self.plan_run('--max-minutes', '2')
        item = result['tasks'][0]
        self.assertEqual((code, result['stop_reason'], result['calls']), (1, 'budget', 1), (result, error))
        self.assertEqual((item['state'], item['reason']), ('interrupted', 'budget'))
        self.assertEqual(self.started()['l1.started'], 0)
        event, = self.budget_events()
        self.assertEqual((event['reason'], event['max_minutes'], event['max_calls'], event['elapsed_ms']),
                         ('max_minutes', '2.0', None, 180000))

    def test_later_tasks_stay_pending_and_are_never_frozen(self):
        self.use_plan('fast', count=2)
        self.actions.write_text(json.dumps([{'README.md': 'First accepted\n'}, {'README.md': 'Second\n'}]))
        code, out, error = self.plan_run('--max-calls', '1', json_output=False)
        self.assertEqual(code, 1, (out, error))
        self.assertIn('Stopped: budget (1 model calls)', out)
        self.assertIn('--resume', out)
        states = {t['task_id']: t['state'] for t in planrun.status(self.main, self.saved, self.log())['tasks']}
        self.assertEqual(states, {'T1': self.accepted_state, 'T2': 'pending'})
        self.assertEqual([r['task'] for r in self.log().records(event='contract.frozen')], ['T1'])
        self.assertEqual(json.loads(self.counter.read_text()), 1)
        # A stop between tasks leaves nothing unfinished: a plain run (no --resume) continues.
        code, result, error = self.plan_run('--max-calls', '1')
        self.assertEqual({t['task_id']: t['state'] for t in result['tasks']},
                         {'T1': self.accepted_state, 'T2': self.accepted_state}, (result, error))

    def test_cli_rejects_invalid_or_misplaced_budget_flags(self):
        for extra in (['--max-calls', '0'], ['--max-calls', '-1'], ['--max-calls', '1.5'], ['--max-calls', 'x'],
                      ['--max-minutes', '0'], ['--max-minutes', 'nan'], ['--max-minutes', '-2']):
            with self.subTest(extra=extra), self.assertRaises(SystemExit) as exit_:
                self.plan_run(*extra)
            self.assertEqual(exit_.exception.code, 2)
        with self.assertRaises(SystemExit) as exit_:
            self.run_cli('plan', '--approve', self.plan_id, '--max-calls', '3', '--repo', str(self.main))
        self.assertEqual(exit_.exception.code, 2)
        self.assertFalse(self.counter.exists())


if __name__ == '__main__':
    unittest.main()
