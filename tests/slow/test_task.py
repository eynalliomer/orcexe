"""Extended end-to-end matrices; enable with --slow or ORCEXE_SLOW_TESTS=1.

Fast examples and pure validators remain in tests.unit.test_task.
"""

import os
import unittest
from tests.unit.test_task import *

class SlowTaskTests(TaskTests):
    test_risk_touch_rejects_inside_allow_before_any_gate = None
    test_precondition_reasons_run_nothing = None
    test_contract_without_any_test_path_is_blocked_before_dispatch = None
    test_approval_record_and_hash_are_required = None
    test_real_plan_publication_and_approval_are_consumable = None
    test_standard_success_freezes_contract_before_executor_and_lands_only_snapshot = None
    test_fast_success_is_accepted = None
    test_project_failure_retries_in_same_worktree_with_feedback = None
    test_exhausted_after_three_and_forged_worktree_status_ignored = None
    test_retry_after_project_check_timeout_without_new_edit_is_accepted = None
    test_fast_retry_after_project_check_timeout_without_new_edit_is_accepted = None
    test_standard_empty_first_attempt_fails_and_unchanged_tree_is_never_accepted = None
    test_fast_empty_first_attempt_fails_and_unchanged_tree_is_never_accepted = None
    test_retry_whose_own_edit_breaks_the_tree_fails = None
    test_contract_tampered_between_attempts_stops = None
    test_contract_tampered_between_attempts_before_next_executor = None
    test_contract_tamper_after_executor_refuses_gate = None
    test_outside_allow_terminal_without_widening = None
    test_outside_allow_risk_map_escalates_to_risky = None
    test_dependency_states_are_scoped_and_latest_wins = None
    test_allowed_new_files_and_deletions_are_checked_and_frozen = None
    test_scope_observes_staged_paths_hidden_by_working_tree = None
    test_executor_commits_are_checked_but_not_used_as_landing_ref = None
    test_controls_are_checked_before_observing_executor_git = None
    test_gate_checks_immutable_own_copy_and_cleans_it_up = None
    test_rehashed_edited_plan_still_needs_matching_approval_record = None
    test_notes_and_feedback_are_fenced_untrusted_data = None
    test_docs_have_every_task_reason_and_exit = None

    def test_inside_allow_risk_touch_is_terminal_before_all_gates(self):
        cases = [('src/auth/new.py', {}, None, ['src/**', 'tests/**']),
                 ('src/private/new.py', {'do_not_touch': [{'path_glob': '**/*private*'}]}, None, ['src/**', 'tests/**']),
                 ('src/private/new.py', {}, 'version: 1\nrisky_paths: [**/*private*]\n', ['src/**', 'tests/**']),
                 ('src/new.py', {}, 'version: 1\nrisky_paths: [src/new.py]\n', ['src/**', 'tests/**']),
                 ('src/new.py', {'risk_map': [{'path_glob': 'src/new.py', 'class': 'auth'}]}, None, ['src/**', 'tests/**']),
                 ('src/new.py', {'risk_map': [{'path_glob': 'src/new.py', 'class': 'payment'}]}, None, ['src/**', 'tests/**']),
                 ('src/new.py', {'risk_map': [{'path_glob': 'src/new.py', 'class': 'data'}]}, None, ['src/**', 'tests/**']),
                 ('src/new.py', {'risk_map': [{'path_glob': 'src/new.py', 'class': 'migration'}]}, None, ['src/**', 'tests/**']),
                 ('src/private/new.py', {'do_not_touch': [{'path_glob': 'src/private'}]}, None, ['src/**', 'tests/**'])]
        for index, (path, project, local, allowed) in enumerate(cases):
            with self.subTest(path=path, project=project, local=local):
                self.saved = self.save([plan_task(goal='Implement a helper', lane='standard', files={'allow': allowed})])
                fsio.atomic_write_json(self.main / '.orcexe/project-map.json', project)
                config = self.main / '.orcexe/risk-map.yaml'
                if local:
                    config.write_text(local)
                else:
                    config.unlink(missing_ok=True)
                if index < 3:
                    # These real future-path scopes can be approved as standard.
                    with mock.patch.dict(os.environ, self.env, clear=True):
                        self.assertEqual(planning.task_lane(self.saved['tasks'][0], project, self.main)[0], 'standard')
                self.actions.write_text(json.dumps([{path: 'value = 1\n'}]))
                with mock.patch.object(gate_lite, 'gate') as fast, \
                        mock.patch.object(task_gate, 'standard_gate') as gate, \
                        mock.patch.object(task_l0, 'run_l0') as l0, \
                        mock.patch.object(task_l1, 'evaluate') as l1:
                    code, result, error = self.invoke()
                self.assertEqual((code, result['state'], result['reason'], result['attempts'], error),
                                 (1, 'rejected', 'risk_path_touched', 1, ''), result)
                self.assertEqual(result['escalation'], dict(suggested_lane='risky', paths=[path]))
                self.assertIsNone(result['land'])
                for check in (fast, gate, l0, l1):
                    check.assert_not_called()
                self.assertEqual(json.loads(self.counter.read_text()), index + 1)
                failed = list(self.log().records(event='gate.failed'))[-1]['data']
                self.assertEqual(failed['reason'], 'risk_path_touched')
                escalated = list(self.log().records(event='lane.escalated'))[-1]['data']
                self.assertEqual((escalated['reason'], escalated['suggested_lane'], escalated['paths']),
                                 ('risk_path_touched', 'risky', [path]))


    def test_scope_observes_committed_then_reverted_paths(self):
        def commit(req):
            path = Path(req.workdir)
            (path / 'README.md').write_text('committed outside change\n')
            self.git_run(path, 'add', 'README.md')
            self.git_run(path, 'commit', '-qm', 'outside change')
            (path / 'README.md').write_text('Example\n')
            self.git_run(path, 'add', 'README.md')
            self.git_run(path, 'commit', '-qm', 'restore outside file')
        code, result, _ = self.invoke(after=commit)
        self.assertEqual((code, result['reason'], result['attempts']), (1, 'outside_allow', 1), result)


    def test_late_worktree_mutation_does_not_change_frozen_land_tree(self):
        real = task_gate.standard_gate
        def mutate(**kwargs):
            receipt = real(**kwargs)
            (Path(kwargs['worktree'].path) / 'src/app.py').write_text('EVIL\n')
            return receipt
        with mock.patch.object(task_gate, 'standard_gate', side_effect=mutate):
            code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual(self.git_run(self.main, 'show', result['land']['ref'] + ':src/app.py'), APP.replace('1', '2'))


    def test_main_advancing_after_gate_prints_manual_cherry_pick(self):
        real = task_gate.standard_gate
        def advance(**kwargs):
            receipt = real(**kwargs)
            self.git_run(self.main, 'commit', '--allow-empty', '-qm', 'main advanced')
            return receipt
        with mock.patch.object(task_gate, 'standard_gate', side_effect=advance):
            code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertIn('cherry-pick ', result['land']['command'])
        self.assertEqual(self.git_run(self.main, 'rev-parse', result['land']['ref'] + '^').strip(), self.head)


    def test_linked_repo_uses_main_state(self):
        linked = self.directory / 'linked'
        self.git_run(self.main, 'worktree', 'add', '-qb', 'linked', str(linked))
        code, result, _ = self.invoke(repo=linked)
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertFalse((linked / '.orcexe').exists())



def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in (SlowTaskTests,))
