"""Extended end-to-end matrices; enable with --slow or ORCEXE_SLOW_TESTS=1.

Fast examples and pure validators remain in tests.unit.test_planning.
"""

import os
import unittest
from tests.unit.test_planning import *

class SlowPlanCLI(PlanCLI):
    test_reused_publisher_rolls_back_at_each_replace = None
    test_symlink_guards_preserve_foreign_targets = None

    test_fake_manager_single_call_and_access_snapshot_routes_and_determinism = None
    test_questions_pending_no_model_or_probes_and_answer_recomputed_for_goal = None
    test_missing_stale_map_without_model = None
    test_real_plan_replayed_with_brace_map_succeeds = None
    test_notes_round_trip_and_rejection_detail_does_not_echo_content = None
    test_approval_paths_and_no_model = None
    test_signal_uses_map_cleanup = None
    test_head_movement_refuses_publication = None

    def test_human_plan_markdown_survives_approval_with_current_state_refresh(self):
        plan = self.proposed()
        path = self.state / 'plan.md'
        path.write_text('Human plan annotation\n')
        code, output, error = self.invoke('--approve', plan['plan_id'])
        self.assertEqual(code, 0, error)
        self.assertIn('human_edit_kept', error)
        self.assertEqual(path.read_text(), 'Human plan annotation\n')
        self.assertIn('Status: approved', (self.state / 'plan.proposed.md').read_text())
        self.assertIn(plan['plan_id'] + ' / approved', (self.state / 'current-state.md').read_text())


    def test_invalid_output_and_p3_preserve_previous_artifacts(self):
        self.proposed()
        before = self.snapshot()
        for output, expected in [('not JSON', 'not_seen: manager_output_invalid\ndetail: output invalid_json\n'),
                                 (json.dumps(manager([task(size='L', done=[])])),
                                  'not_seen: plan_rejected\nviolation: empty_done T1\nviolation: size_l T1\n')]:
            self.final.write_text(output)
            self.assertEqual(self.invoke('--goal', 'tests pass'), (2, '', expected))
            self.assertEqual(self.snapshot(), before)


    def test_legacy_unusable_protective_globs_publish_a_risky_plan(self):
        for section in ('risk_map', 'do_not_touch'):
            self.value = project(self.head)
            self.value[section] = [dict(path_glob='src/{broken', **{'class': 'other'})]
            self.write_map()
            result = self.proposed()
            self.assertEqual(result['tasks'][0]['lane'], 'risky')
            self.assertIn('cli:risk_glob_unusable:src/{broken', result['tasks'][0]['lane_basis'])


    def test_step_failures_have_distinct_content_free_reasons(self):
        self.proposed()
        before = self.snapshot()
        cases = [(planning, 'parse_manager', 'manager_output_invalid', 2, 'parse_manager'),
                 (planning, 'checks', 'plan_rejected', 2, 'checks'),
                 (planning, 'task_lane', 'internal_error', 70, 'task_lane'),
                 (planning, 'publish', 'write_failed', 2, None),
                 (questions, 'select', 'internal_error', 70, 'select_questions')]
        for module, method, reason, code, step in cases:
            with self.subTest(step=method), mock.patch.object(module, method, side_effect=RuntimeError('PRIVATE_CONTENT')):
                expected = 'not_seen: ' + reason + '\n'
                if step:
                    expected += 'detail: ' + step + ' RuntimeError\n'
                self.assertEqual(self.invoke('--goal', 'tests pass'), (code, '', expected))
                self.assertEqual(self.snapshot(), before)
        original_route = route.route
        for role, step in (('manager', 'manager_route'), ('executor', 'executor_route')):
            def fail(selected_role, *args, **kwargs):
                if selected_role == role:
                    raise ValueError('PRIVATE_CONTENT')
                return original_route(selected_role, *args, **kwargs)
            with self.subTest(role=role), mock.patch.object(route, 'route', side_effect=fail):
                self.assertEqual(self.invoke('--goal', 'tests pass'),
                                 (70, '', 'not_seen: internal_error\ndetail: ' + step + ' ValueError\n'))
                self.assertEqual(self.snapshot(), before)


    def test_goal_budget_four_answers_across_forms_then_plan_proceeds(self):
        goal = 'Fix 1 typo in README.md'
        self.value['unknowns'] = [unknown('README choice ' + str(n)) for n in range(10)]
        self.write_map()
        for n in range(4):
            code, out, err = self.invoke('--goal', goal, command='questions')
            self.assertEqual((code, err), (0, ''))
            form = json.loads(out)
            self.assertEqual(len(form['questions']), 4 - n)
            qid = form['questions'][0]['id']
            self.assertEqual(self.invoke('--form', form['form_id'], '--choice', qid + '=0', command='answer')[0], 0)
        decisions = fsio.read_jsonl(self.state / 'decisions.jsonl')
        self.assertEqual({d['goal_hash'] for d in decisions}, {questions.goal_hash(goal)})
        result = self.proposed()
        self.assertEqual(len(self.model_calls()), 1)
        self.assertEqual(len(result['decisions']), 4)
        self.assertEqual([a['reason'] for a in result['assumptions']].count('over_limit'), 6)
        # A different goal gets a fresh budget for the six still-undecided candidates.
        self.assertEqual(self.invoke('--goal', 'README must improve')[2], 'not_seen: questions_pending\n')
        self.assertEqual(len(self.model_calls()), 1)


    def test_executor_without_eligible_tool_is_not_error(self):
        access = [route.Access('claude-code', 'anthropic', True, 'ok',
                  {'claude-opus-5-5': ('high',)}, None, None)]
        with mock.patch.object(route, 'gather_access', return_value=access):
            result = self.proposed()
        selection = result['tasks'][0]['route']
        self.assertIsNone(selection['tool'])
        self.assertEqual(selection['reason'], 'no_eligible_candidate')
        self.assertIn('Unassigned: no_eligible_candidate', (self.state / 'plan.md').read_text())


    def test_risk_upgrades_route_each_task_from_one_snapshot(self):
        self.value['risk_map'] = [{'path_glob': 'shop/pay.py', 'class': 'payment'}]
        self.value['do_not_touch'] = [{'path_glob': 'src/app.py'}]
        self.write_map()
        # Goal-scoped risk confirmations are covered by the eventual plan approval.
        code, out, err = self.invoke('--goal', 'shop payment must work', command='questions')
        self.assertEqual((code, err), (0, ''))
        form = json.loads(out)
        self.assertEqual(form['questions'], [])
        self.assertEqual({a['reason'] for a in form['assumptions']}, {'deferred_to_plan'})
        self.final.write_text(json.dumps(manager([
            task(files={'allow': ['shop/*.py', 'tests/test_shop.py']}),
            task('T2', files={'allow': ['src/app.py', 'tests/test_app.py']}, goal='Add a helper'),
            task('T3', lane='standard'),
        ])))
        gather = route.gather_access
        with mock.patch.object(route, 'gather_access', wraps=gather) as snapshot:
            result = self.proposed()
        snapshot.assert_called_once()
        self.assertEqual([t['lane'] for t in result['tasks']], ['risky', 'risky', 'standard'])
        self.assertEqual([t['route']['lane'] for t in result['tasks']], ['risky', 'risky', 'standard'])
        self.assertIn('cli:risk_map:payment:shop/pay.py', result['tasks'][0]['lane_basis'])
        self.assertIn('cli:do_not_touch:src/app.py', result['tasks'][1]['lane_basis'])
        markdown = (self.state / 'plan.md').read_text()
        self.assertIn('touches payment area shop/pay.py', markdown)
        self.assertIn('touches do-not-touch area src/app.py', markdown)


    def test_manager_unavailable_reasons(self):
        for reason in ('no_route', 'stalled', 'timeout', 'auth', 'quota', 'failed'):
            with mock.patch.object(mapping, 'manager_run', side_effect=mapping.ManagerUnavailable(reason)):
                self.assertEqual(self.invoke('--goal', 'tests pass'),
                                 (2, '', 'not_seen: manager_unavailable: ' + reason + '\n'))
        with mock.patch.object(route, 'gather_access', return_value=[]):
            self.assertEqual(self.invoke('--goal', 'tests pass')[2], 'not_seen: manager_unavailable: no_route\n')


    def test_approval_appends_and_write_failure_rolls_back(self):
        result = self.proposed()
        fsio.append_jsonl(self.state / 'decisions.jsonl', dict(schema='orcexe.decision.v1',
                          question_id='q-original', answer='keep', kind='text'))
        before = self.snapshot()
        with mock.patch.object(mapping, 'publish', side_effect=OSError('disk full')):
            self.assertEqual(self.invoke('--approve', result['plan_id']), (2, '', 'not_seen: write_failed\n'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.invoke('--approve', result['plan_id'])[0], 0)
        records = fsio.read_jsonl(self.state / 'decisions.jsonl')
        self.assertEqual([r['kind'] for r in records], ['text', 'plan_approval'])


    def test_invalid_approval_document_and_legacy_plan_publication(self):
        for text in ('not JSON', 'null', '[]', '{}'):
            (self.state / 'plan.json').write_text(text)
            before = self.snapshot()
            self.assertEqual(self.invoke('--approve', 'wrong'), (2, '', 'not_seen: state_corrupt\n'))
            self.assertEqual(self.snapshot(), before)
        self.proposed()
        original = self.snapshot()
        for name in ('plan.json', 'plan.md'):
            (self.state / name).unlink()
            (self.state / name).write_bytes(original[name])
        result = self.proposed()
        self.assertEqual(self.invoke('--approve', result['plan_id'])[0], 0)


    def test_approval_failure_with_no_ledger_leaves_none(self):
        result = self.proposed()
        before = self.snapshot()
        with mock.patch.object(mapping, 'publish', side_effect=OSError('disk full')):
            self.assertEqual(self.invoke('--approve', result['plan_id']), (2, '', 'not_seen: write_failed\n'))
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.state / 'decisions.jsonl').exists())


    def test_map_and_plan_publications_are_independent(self):
        outputs = dict((name, 'old ' + name) for name in ('project-map.md', 'history.md', 'direction.md'))
        outputs['project-map.json'] = json.dumps(self.value)
        mapping.publish(self.state, outputs)
        self.proposed()
        saved = (self.state / 'plan.json').read_bytes()
        outputs['history.md'] = 'refreshed history'
        mapping.publish(self.state, outputs)
        self.assertEqual((self.state / 'plan.json').read_bytes(), saved)
        self.proposed()
        for name, text in outputs.items():
            self.assertEqual((self.state / name).read_text(), text)






    def test_cleaned_empty_text_and_allow_globs_are_rejected_without_publication(self):
        self.proposed()
        before = self.snapshot()
        for changes, violation in [({'goal': ''}, 'empty_goal'), ({'goal': 'ㅤ'}, 'empty_goal'),
                                    ({'done': ['\u200b']}, 'empty_done'),
                                    ({'files': {'allow': []}}, 'empty_allow'),
                                    ({'files': {'allow': ['person@example.com/*.py']}}, 'invalid_glob')]:
            self.final.write_text(json.dumps(manager([task(**changes)])))
            code, out, err = self.invoke('--goal', 'tests pass')
            self.assertEqual((code, out), (2, ''), violation)
            self.assertIn('violation: ' + violation + ' T1', err)
            self.assertEqual(self.snapshot(), before)
        self.final.write_text('```json\n' + json.dumps(manager([task(
            files={'allow': ['src/\u200bapp.py', 'tests/test_app.py']})])) + '\n```')
        result = self.proposed()
        self.assertEqual(result['tasks'][0]['files']['allow'], ['src/app.py', 'tests/test_app.py'])
        markdown = (self.state / 'plan.md').read_text()
        for expected in ('Allowed files', 'Size', 'Parallel', 'src/app.py', '| S | false |'):
            self.assertIn(expected, markdown)


    def test_no_test_path_after_lane_raise_preserves_all_saved_outputs(self):
        self.proposed()
        before = self.snapshot()
        for requested in ('fast', 'standard', 'risky'):
            self.final.write_text(json.dumps(manager([task(goal='Implement a helper', lane=requested,
                                                           files={'allow': ['src/*.py']})])))
            self.assertEqual(self.invoke('--goal', 'tests pass'),
                             (2, '', 'not_seen: plan_rejected\nviolation: no_test_path T1\n'))
            self.assertEqual(self.snapshot(), before)
        # A source comment task that remains fast needs no test path.
        self.final.write_text(json.dumps(manager([task(goal='Add a comment to src/app.py',
                                                       files={'allow': ['src/app.py']})])))
        self.assertEqual(self.proposed()['tasks'][0]['lane'], 'fast')
        self.final.write_text(json.dumps(manager([task(lane='standard', files={'allow': ['docs/*.md']})])))
        self.assertEqual(self.proposed()['tasks'][0]['lane'], 'standard')


    def test_local_risk_map_matches_do_words_and_paths_and_is_rechecked(self):
        from orcexe import classify
        config = self.state / 'risk-map.yaml'
        config.write_text('version: 1\nrisky_words: [ledger]\nrisky_paths: [Shop/Étage.py]\n')
        for candidate, signal in [(task(goal='Update ledger'), 'risk_word:ledger'),
                                  (task(files={'allow': ['shop/E\u0301TAGE.py', 'tests/test_shop.py']}), 'risk_path:Shop/Étage.py')]:
            self.final.write_text(json.dumps(manager([candidate])))
            result = self.proposed()
            self.assertEqual(result['tasks'][0]['lane'], 'risky')
            self.assertIn('cli:' + signal, result['tasks'][0]['lane_basis'])
            if 'ledger' in candidate['goal']:
                self.assertEqual(classify.classify_repo(str(self.main), candidate['goal']).lane, 'risky')
        self.final.write_text(json.dumps(manager()))
        result = self.proposed()
        config.write_text('version: 1\nrisky_paths: [README.md]\n')
        before = self.snapshot()
        self.assertEqual(self.invoke('--approve', result['plan_id']), (2, '', 'not_seen: plan_tampered\n'))
        self.assertEqual(self.snapshot(), before)


    def test_approval_binds_all_content_and_rechecks_rehashed_invalid_tasks(self):
        original = self.proposed()
        self.assertEqual(original['content_sha256'], planning.content_sha256(original))
        self.assertEqual(original['plan_id'], planning.plan_id(original['content_sha256']))
        changes = [lambda p: p['tasks'][0].update(goal='Different goal'),
                   lambda p: p['tasks'][0]['files'].update(allow=['src/app.py']),
                   lambda p: p['tasks'][0].update(done=['Changed criterion']),
                   lambda p: p['tasks'][0].update(lane='risky'),
                   lambda p: p['tasks'][0]['route'].update(tool='changed'),
                   lambda p: p.update(assumptions=[{'changed': True}])]
        for change in changes:
            plan = copy.deepcopy(original)
            change(plan)
            (self.state / 'plan.json').write_text(json.dumps(plan))
            before = self.snapshot()
            self.assertEqual(self.invoke('--approve', original['plan_id']), (2, '', 'not_seen: plan_tampered\n'))
            self.assertEqual(self.snapshot(), before)
        for change in [lambda p: p.update(tasks=[]),
                       lambda p: p['tasks'][0].update(done=['\u200b']),
                       lambda p: p['tasks'][0].update(goal='ㅤ'),
                       lambda p: p['tasks'][0].update(size='L'),
                       lambda p: p['tasks'][0].update(depends_on=['T2']),
                       lambda p: p['tasks'][0].update(parallel='yes'),
                       lambda p: p['tasks'][0]['files'].update(allow=[]),
                       lambda p: p['tasks'][0].update(lane='standard', files={'allow': ['src/app.py']}),
                       lambda p: p['tasks'][0].update(lane='risky'),
                       lambda p: p['tasks'][0]['lane_basis'].append('forged')]:
            plan = copy.deepcopy(original)
            change(plan)
            plan['content_sha256'] = planning.content_sha256(plan)
            plan['plan_id'] = planning.plan_id(plan['content_sha256'])
            (self.state / 'plan.json').write_text(json.dumps(plan))
            before = self.snapshot()
            self.assertEqual(self.invoke('--approve', plan['plan_id']), (2, '', 'not_seen: plan_tampered\n'))
            self.assertEqual(self.snapshot(), before)
        (self.state / 'plan.json').write_text(json.dumps(original))
        self.assertEqual(self.invoke('--approve', original['plan_id'])[0], 0)
        approved = fsio.read_json(self.state / 'plan.json')
        self.assertEqual(planning.content_sha256(approved), original['content_sha256'])
        self.assertEqual(fsio.read_jsonl(self.state / 'decisions.jsonl')[-1]['content_sha256'],
                         original['content_sha256'])


    def test_corrupt_approval_state_is_state_corrupt_and_preserved(self):
        result = self.proposed()
        for text in ('{broken', '\ufffd', 'null', '[]', '{}'):
            (self.state / 'plan.json').write_text(text)
            before = self.snapshot()
            self.assertEqual(self.invoke('--approve', result['plan_id']), (2, '', 'not_seen: state_corrupt\n'))
            self.assertEqual(self.snapshot(), before)
        (self.state / 'plan.json').write_text(json.dumps(result))
        for text in ('{broken\n', 'null\n', '{}\n'):
            (self.state / 'decisions.jsonl').write_text(text)
            before = self.snapshot()
            self.assertEqual(self.invoke('--approve', result['plan_id']), (2, '', 'not_seen: state_corrupt\n'))
            self.assertEqual(self.snapshot(), before)


    def test_linked_worktree_shares_main_root_form_decisions_and_plan(self):
        linked = self.directory / 'linked'
        self.git_run(self.main, 'worktree', 'add', '-qb', 'linked', str(linked))
        (linked / 'README.md').write_text('Linked tree must not be observed\n')
        self.git_run(linked, 'commit', '-am', 'different linked head', '-q')
        # A conflicting local state must never be selected.
        (linked / '.orcexe').mkdir()
        (linked / '.orcexe/project-map.json').write_text('{}')
        with mock.patch.dict(os.environ, self.env, clear=True):
            repo, state, head = questions.context(type('Args', (), {'repo': linked})())
        self.assertEqual((repo, state, head), (self.main, self.state, self.head))
        self.value['unknowns'] = [unknown('Which README format?')]
        self.write_map()
        code, out, err = self.invoke('--goal', 'Fix 1 typo in README.md', command='questions', repo=linked)
        self.assertEqual((code, err), (0, ''))
        form = json.loads(out)
        qid = form['questions'][0]['id']
        self.assertEqual(self.invoke('--form', form['form_id'], '--text', qid + '=Keep format',
                                     command='answer', repo=linked)[0], 0)
        code, out, err = self.invoke('--goal', 'Fix 1 typo in README.md', repo=linked)
        self.assertEqual((code, err), (0, ''))
        plan = json.loads(out)
        self.assertEqual(plan['head'], self.head)
        self.assertEqual(plan['decisions'], [qid])
        self.assertEqual(self.invoke('--approve', plan['plan_id'], repo=linked)[0], 0)
        self.assertFalse((linked / '.orcexe/plan.json').exists())



def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in (SlowPlanCLI,))
