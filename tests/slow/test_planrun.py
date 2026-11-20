"""Extended end-to-end matrices; enable with --slow or ORCEXE_SLOW_TESTS=1.

Fast examples and pure validators remain in tests.unit.test_planrun.
"""

import os
import unittest
from tests.unit.test_planrun import *

class SlowPlanRunTests(PlanRunTests):
    test_stacked_topological_order_dependency_visibility_and_finished_noop = None
    test_single_dependent_task_uses_same_integration_chain = None
    test_failure_blocks_only_transitive_dependents_and_risky_lanes = None
    test_interrupt_closes_attempt_removes_worktree_and_resume_keeps_contract = None
    test_resume_never_replenishes_attempt_budget = None
    test_resume_rejects_tampered_contract_without_refreezing_or_dispatch = None
    test_signal_during_worktree_creation_is_cleaned_before_resume = None
    test_plan_preconditions_dispatch_nothing = None
    test_status_is_read_only_and_ignores_markdown_and_live_lock = None
    test_docs_have_planrun_reasons_and_commands = None

    def test_l1_failure_then_acceptance_uses_existing_retry_pipeline(self):
        test_task_l1.EvaluatorTests.evaluator(self, [rubric(False), rubric()])
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
                                            'tests/test_app.py': TEST.replace('1', '2')},
                                           {'tests/test_app.py': TEST.replace('1', '2') + '\n# edge case\n'}]))
        code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['state']), (0, 'completed'), result)
        self.assertEqual(result['tasks'][0]['attempts'], 2)
        self.assertIn('missing edge case', self.briefs[1])
        self.assertEqual([r['data']['criteria'] for r in self.log().records(event='l1.verdict')],
                         [['d1:fail'], ['d1:pass']])
        self.assertEqual(len(list(self.log().records(event='contract.frozen'))), 1)


    def test_exhausted_and_gate_passed_are_not_dependencies(self):
        for failure in ('exhausted', 'gate_passed'):
            with self.subTest(failure=failure):
                self.saved = self.save([plan_task(lane='standard', files={'allow': ['src/*.py', 'tests/**']}),
                                        plan_task('T2', depends_on=['T1']), plan_task('T3')], scenario=failure)
                self.actions.write_text(json.dumps([{'src/app.py': APP + '\n'}] if failure == 'exhausted' else
                    [{'src/app.py': APP.replace('1', '2'), 'tests/test_app.py': TEST.replace('1', '2')}]))
                self.requests.clear()
                code, result, _ = self.invoke_plan()
                self.assertEqual((code, result['tasks'][0]['state']), (1, failure), result)
                self.assertEqual(result['tasks'][1], dict(task_id='T2', state='blocked', reason='dependency_failed', attempts=0))
                self.assertEqual([r.task for r in self.requests], ['T1'] * (3 if failure == 'exhausted' else 1) + ['T3'])


    def test_signal_on_executor_stream_closes_running_child_and_resumes(self):
        self.docs([plan_task()])
        wrapper = Path(self.env['ORCEXE_BIN_CODEX'])
        original_wrapper = wrapper.read_text()
        wrapper.write_text(original_wrapper.replace("'ORCEXE_FAKE_SCENARIO': 'success'",
                                                    "'ORCEXE_FAKE_SCENARIO': 'stall'"))
        real = proc.run
        signalled = []
        def run(argv, **options):
            if 'exec' in argv and argv[0] == str(wrapper):
                callback = options['on_line']
                def on_line(line):
                    callback(line)
                    if not signalled:
                        # The real child has flushed an event; the ledger's
                        # attempt is open. No sleeps or timing assertions.
                        signalled.append(line)
                        self.assertEqual(len(list(self.log().records(event='attempt.started'))), 1)
                        os.kill(os.getpid(), signal.SIGTERM)
                options['on_line'] = on_line
            return real(argv, **options)
        with mock.patch.object(proc, 'run', side_effect=run):
            code, result, _ = self.invoke_plan()
        self.assertTrue(signalled)
        self.assertEqual((code, result['state']), (130, 'interrupted'), result)
        ended = list(self.log().records(event='attempt.ended'))
        self.assertEqual(len(ended), 1)
        self.assertEqual(ended[0]['data']['status'], 'cancelled')
        self.assertFalse(Path(self.requests[0].workdir).exists())
        self.assertEqual(worktree.list_managed(self.main), [])
        wrapper.write_text(original_wrapper)
        code, result, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, result['tasks'][0]['attempts']), (0, 2), result)
        self.assertEqual(len(list(self.log().records(event='attempt.ended'))), 2)


    def test_signal_during_acceptance_keeps_tip_and_ledger_consistent(self):
        self.docs([plan_task(), plan_task('T2', depends_on=['T1'])])
        real = planrun.advance
        def advance(*args):
            real(*args)
            os.kill(os.getpid(), signal.SIGINT)
        with mock.patch.object(planrun, 'advance', side_effect=advance):
            code, result, _ = self.invoke_plan()
        self.assertEqual((code, result['state']), (130, 'interrupted'), result)
        self.assertEqual([t['state'] for t in result['tasks']], ['accepted', 'pending'])
        self.assertEqual(len(list(self.log().records(task='T1', event='task.state'))), 1)
        code, result, _ = self.invoke_plan(resume=True)
        self.assertEqual((code, result['state']), (0, 'completed'), result)
        self.assertEqual([r.task for r in self.requests], ['T1', 'T2'])
        self.assertEqual(len(list(self.log().records(task='T1', event='task.state'))), 1)



def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in (SlowPlanRunTests,))
