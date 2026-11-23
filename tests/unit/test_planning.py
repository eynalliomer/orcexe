"""Offline P1–P3 and approval contract, using only fake CLI wrappers."""
from contextlib import redirect_stderr, redirect_stdout
import copy
import io
import json
import os
import re
from pathlib import Path
import signal
import sys
import unittest
from unittest import mock

from tests.unit.test_gitutil import GitFixture, ROOT
from tests.unit import test_mapping
from tests.unit.test_questions import project, unknown
from orcexe import cli, exitcodes, fsio, mapping, planning, questions, route


def task(identifier='T1', **changes):
    result = dict(id=identifier, goal='Fix the typo in README.md', files={'allow': ['README.md']},
                  done=['The spelling is corrected.'], depends_on=[], lane='fast', size='S', parallel=False)
    result.update(changes)
    return result


def manager(tasks=None):
    return dict(schema='orcexe.manager.plan.v1', tasks=[task()] if tasks is None else tasks)


class PlanChecks(unittest.TestCase):
    def test_every_p3_violation(self):
        cases = [([task(goal='ㅤ')], 'empty_goal'), ([task(files={'allow': []})], 'empty_allow'),
                 ([task(done=[])], 'empty_done'), ([task(done=[' '])], 'empty_done'),
                 ([task(), task()], 'duplicate_id'), ([task(depends_on=['T2'])], 'unknown_dependency'),
                 ([task(depends_on=['T1'])], 'cycle'), ([task(size='L')], 'size_l'),
                 ([task('T' + str(n)) for n in range(1, 14)], 'too_many_tasks'),
                 ([], 'no_tasks'), ([task(files={'allow': ['../outside']})], 'invalid_glob'),
                 ([task(parallel=True), task('T2', parallel=True)], 'parallel_overlap'),
                 ([task(lane='standard', files={'allow': ['src/app.py']})], 'no_test_path')]
        self.assertEqual({code for _, code in cases}, set(planning.VIOLATION_CODES))
        for tasks, expected in cases:
            with self.subTest(expected=expected):
                parsed = planning.parse_manager(json.dumps(manager(tasks)))
                self.assertIn(expected, [code for code, _ in planning.checks(parsed['tasks'])])
        cycle = [task(depends_on=['T2']), task('T2', depends_on=['T3']), task('T3', depends_on=['T1'])]
        self.assertEqual(planning.checks(cycle), [('cycle', 'T1'), ('cycle', 'T2'), ('cycle', 'T3')])

    def test_test_path_uses_l0_rules_on_literals_and_covered_tracked_files(self):
        cases = [(['src/*.py'], (), True),
                 (['src/*.py', 'tests/**'], (), True),
                 (['src/*.py', 'tests/**'], ('tests/test_app.py',), False),
                 (['src/*.py', 'tests/test_new.py'], (), False),
                 (['src/*.ts', 'src/*.spec.ts'], (), False),
                 (['docs/*.md', 'LICENSE'], (), False),
                 (['src'], ('src/app.py', 'src/app.test.js'), False),
                 (['*'], ('src/test_app.py',), True)]
        for lane in ('standard', 'risky', 'fast'):
            for allowed, tracked, missing in cases:
                with self.subTest(lane=lane, allowed=allowed, tracked=tracked):
                    violations = planning.checks([task(lane=lane, files={'allow': allowed})], tracked=tracked)
                    self.assertEqual(('no_test_path', 'T1') in violations, missing and lane != 'fast')

    def test_strict_schema(self):
        text = json.dumps(manager())
        self.assertEqual(planning.parse_manager(' \n' + text), manager())
        invalid = ['prose ' + text, text + text, 'null', '[]', text + ' tail',
                   text[:-1] + ',"schema":"orcexe.manager.plan.v1"}',
                   ' ' * mapping.OUTPUT_CAP + text]
        for change in ({'extra': 1}, {'schema': 'wrong'}, {'tasks': {}}, {'tasks': [True]}):
            invalid.append(json.dumps(dict(manager(), **change)))
        for key in task():
            changed = task()
            del changed[key]
            invalid.append(json.dumps(manager([changed])))
        for change in ({'id': 'X1'}, {'goal': True}, {'files': []},
                       {'files': {'allow': ['x'], 'deny': []}}, {'done': [1]},
                       {'depends_on': 'T2'}, {'lane': 'any'}, {'lane': {}}, {'size': 'XL'},
                       {'parallel': 1}, {'route': {}}, {'files': {'allow': [1]}}):
            invalid.append(json.dumps(manager([task(**change)])))
        for value in invalid:
            with self.subTest(value=value[:120]), self.assertRaisesRegex(ValueError, 'manager_output_invalid'):
                planning.parse_manager(value)

    def test_field_caps_and_content_free_first_failure(self):
        valid = task(goal='界' * 300, done=['x' * 300] * 8,
                     files={'allow': ['x' * 200]}, notes='n' * 2000)
        self.assertEqual(planning.parse_manager(json.dumps(manager([valid])))['tasks'], [valid])
        for change, detail in [({'goal': 'PRIVATE' + 'x' * 1378}, 'tasks[0].goal too_long (1385 > 300)'),
                               ({'done': ['x' * 301]}, 'tasks[0].done[0] too_long (301 > 300)'),
                               ({'done': ['x'] * 9}, 'tasks[0].done too_many (9 > 8)'),
                               ({'files': {'allow': ['x' * 201]}},
                                'tasks[0].files.allow[0] too_long (201 > 200)'),
                               ({'notes': 'x' * 2001}, 'tasks[0].notes too_long (2001 > 2000)')]:
            with self.subTest(change=detail), self.assertRaises(mapping.ManagerOutputInvalid) as caught:
                planning.parse_manager(json.dumps(manager([task(**change)])))
            self.assertEqual(caught.exception.detail, detail)
            self.assertEqual(str(caught.exception), 'manager_output_invalid')
        with self.assertRaises(mapping.ManagerOutputInvalid) as caught:
            planning.parse_manager(json.dumps(manager([task(goal='x' * 301, notes='x' * 2001)])))
        self.assertEqual(caught.exception.detail, 'tasks[0].goal too_long (301 > 300)')

    def test_plan_brief_declares_all_caps_and_optional_notes(self):
        brief = planning.build_brief('goal', project(), {'assumptions': []}, [])
        for expected in ('<=262144 UTF-8 bytes', 'Strings <=4000', 'lists <=100',
                         'one sentence <=300', 'done item <=300', 'done <=8 items',
                         'glob <=200', 'Optional task notes <=2000', 'at most 12 tasks',
                         '<=256 alternatives', '<=4096 total characters', 'no_test_path', 'documentation-only'):
            self.assertIn(expected, brief)

    def test_glob_intersections_include_future_paths(self):
        for left, right, expected in [
            ('src/*.py', 'src/new.py', True), ('src/*.py', 'src/sub/new.py', False),
            ('src/**', 'src/new/deep.py', True), ('**/*.py', 'new.py', True),
            ('src/{app,test}/*.py', 'src/test/f*.py', True),
            ('src/a*.py', 'src/b*.py', False), ('a?.py', 'ab.*', True),
            ('[a-c].py', '[d-z].py', False), ('[!a-c].py', 'z.py', True),
            ('[!a-c].py', 'a.py', False), ('**/x/**', '**/y/**', True),
            ('[!]].py', 'a.py', True), ('[!]].py', '[]].py', False),
            ('a/**/z', 'a/z', True), ('*', '*', True), ('**', 'future/path', True),
            ('a/**/x', 'b/**/x', False), ('a[0-9].py', 'a[5-8].*', True)]:
            with self.subTest(left=left, right=right):
                self.assertEqual(planning.overlaps(left, right), expected)
                self.assertEqual(planning.overlaps(right, left), expected)
        for pattern in ('', '/root', 'a/../b', 'a//b', './x', 'a\\b',
                        'C:/x', 'x\n', '{a,../b}', '{a,b', 'a}', '[x', '[z-a]', 'a**b'):
            with self.subTest(pattern=pattern), self.assertRaises(ValueError):
                planning.expand(pattern)

    def test_unusable_risk_globs_raise_lane_with_auditable_basis(self):
        for section in ('risk_map', 'do_not_touch'):
            for pattern in ('src/{broken', '{a,b}' * 9, '{a,b}' * 8):
                value = project()
                value[section] = [dict(path_glob=pattern, **{'class': 'data'})]
                lane, basis = planning.task_lane(task(files={'allow': ['z' * 200]}), value)
                self.assertEqual(lane, 'risky')
                self.assertIn('cli:risk_glob_unusable:' + pattern, basis)

    def test_excluded_protective_globs_raise_only_on_overlap(self):
        value = project()
        value['do_not_touch'] = [dict(path_glob=p) for p in ('.orcexe/**', 'vendor/**', '.env*')]
        lane, basis = planning.task_lane(task(files={'allow': ['src/cli.py']}, lane='standard'), value)
        self.assertEqual(lane, 'standard')
        self.assertFalse(any('risk_glob_unusable' in b for b in basis))
        for path, pattern in (('.env.example', '.env*'), ('.orcexe/cache.json', '.orcexe/**'),
                              ('vendor/lib.py', 'vendor/**')):
            with self.subTest(path=path):
                lane, basis = planning.task_lane(task(files={'allow': [path]}), value)
                self.assertEqual(lane, 'risky')
                self.assertIn('cli:do_not_touch:' + pattern, basis)

    def test_transitive_dependencies_allow_parallel_overlap(self):
        tasks = [task(parallel=True), task('T2', depends_on=['T1']),
                 task('T3', parallel=True, depends_on=['T2'])]
        self.assertEqual(planning.checks(tasks), [])
        tasks[-1]['depends_on'] = []
        self.assertEqual(planning.checks(tasks), [('parallel_overlap', 'T1')])
        tasks[-1]['files']['allow'] = ['src/new.py']
        self.assertEqual(planning.checks(tasks), [])

    def test_lane_raise_never_lower_and_basis(self):
        for manager_lane in planning.LANES:
            for goal, expected in [('Fix the typo in README.md', manager_lane),
                                   ('Implement a helper', max(manager_lane, 'standard', key=planning.LANES.index)),
                                   ('Change authentication', 'risky')]:
                lane, basis = planning.task_lane(task(goal=goal, lane=manager_lane), project())
                self.assertEqual(lane, expected)
                self.assertIn('manager:' + manager_lane, basis)
                self.assertTrue(any(v.startswith('cli:classify:') for v in basis))
        for risk in ('auth', 'payment', 'data', 'migration'):
            p = project()
            p['risk_map'] = [{'path_glob': 'shop/pay.py', 'class': risk}]
            lane, basis = planning.task_lane(task(files={'allow': ['shop/*.py']}), p)
            self.assertEqual(lane, 'risky')
            self.assertIn('cli:risk_map:' + risk + ':shop/pay.py', basis)
        p = project()
        p['do_not_touch'] = [{'path_glob': 'README.md'}]
        lane, basis = planning.task_lane(task(), p)
        self.assertEqual(lane, 'risky')
        self.assertIn('cli:do_not_touch:README.md', basis)
        p['do_not_touch'] = []
        p['risk_map'] = [{'path_glob': 'README.md', 'class': 'infra'}]
        self.assertEqual(planning.task_lane(task(), p)[0], 'fast')
        self.assertEqual(planning.task_lane(task(files={'allow': ['src/auth/new.py']}), project())[0], 'risky')

    def test_lane_v2_done_and_notes_raise_with_field_basis(self):
        # T58: every task's lane comes from goal + done + notes (lane.v2).
        lane, basis = planning.task_lane(task(done=['The spelling is corrected.',
                                                    'The branch is pushed to origin.']), project())
        self.assertEqual(lane, 'risky')
        self.assertIn('cli:done[1]:A8:pushed', basis)
        lane, basis = planning.task_lane(task(notes='Cleanup uses shutil.rmtree(path).'), project())
        self.assertEqual(lane, 'risky')
        self.assertIn('cli:notes:A2:shutil.rmtree', basis)
        lane, basis = planning.task_lane(task(goal='Count fixes in fix_commits'), project())
        self.assertEqual(lane, 'risky')
        self.assertIn('cli:goal:outward_action:commit', basis)
        self.assertEqual(planning.task_lane(task(), project())[0], 'fast')
        self.assertEqual(planning.task_lane(task(notes='Remove the stale draft.'), project())[0], 'standard')

    def test_documented_p3_violations_match_constants(self):
        tables = []
        for path in (ROOT / 'README.md', ROOT / 'plugins/orcexe/skills/orcexe/SKILL.md'):
            table = path.read_text().split('<!-- plan-violation-reasons -->\n')[1].split('<!-- /plan-violation-reasons -->')[0]
            self.assertEqual({code for line in table.splitlines() if line.startswith('| `')
                              for code in re.findall(r'`([^`]+)`', line.split('|')[1])},
                             set(planning.VIOLATION_CODES))
            tables.append(table)
        self.assertEqual(*tables)

    def test_documented_exit_reasons_match_constants(self):
        docs = [ROOT / 'README.md', ROOT / 'plugins/orcexe/skills/orcexe/SKILL.md']
        tables = []
        for path in docs:
            text = path.read_text()
            table = text.split('<!-- plan-exit-reasons -->\n')[1].split('<!-- /plan-exit-reasons -->')[0]
            rows = [line for line in table.splitlines() if line.startswith('| `')]
            actual = {line.split('`')[1]: int(line.split('|')[2].strip()) for line in rows}
            self.assertEqual(actual, planning.EXIT_REASONS)
            tables.append(table)
        self.assertEqual(*tables)


class PlanCLI(GitFixture):
    def setUp(self):
        super().setUp()
        (self.main / 'README.md').write_text('Example\n')
        (self.main / 'src').mkdir()
        (self.main / 'src/app.py').write_text('def run():\n    pass\n')
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '-qm', 'seed')
        self.head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        self.state = self.main / '.orcexe'
        self.value = project(self.head)
        self.write_map()
        self.final = self.directory / 'final-input'
        self.calls = self.directory / 'calls.jsonl'
        self.final.write_text(json.dumps(manager()))
        self.env['ORCEXE_STATE_DIR'] = str(self.home / 'state')
        for tool, executable in (('claude-code', 'claude'), ('codex', 'codex'), ('cursor', 'agent')):
            wrapper = self.directory / ('fake-' + executable)
            settings = dict(ORCEXE_FAKE_FINAL_TEXT_FILE=str(self.final), ORCEXE_FAKE_CALLS=str(self.calls))
            wrapper.write_text('#!' + sys.executable + '\nimport os,sys,runpy\n'
                               'from pathlib import Path\nos.environ.update(' + repr(settings) + ')\n'
                               'if "exec" in sys.argv or "-p" in sys.argv:\n'
                               '    assert (Path.cwd()/"README.md").read_text() == "Example\\n"\n'
                               '    assert not (Path.cwd()/".orcexe").exists()\n'
                               'sys.path.insert(0, ' + repr(str(test_mapping.FAKES)) + ')\n'
                               'runpy.run_path(' + repr(str(test_mapping.FAKES / executable)) + ', run_name="__main__")\n')
            wrapper.chmod(0o700)
            self.env['ORCEXE_BIN_' + tool.upper().replace('-', '_')] = str(wrapper)

    def write_map(self):
        fsio.atomic_write_json(self.state / 'project-map.json', self.value)

    def invoke(self, *options, command='plan', repo=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out), redirect_stderr(err):
            code = cli.main([command, '--repo', str(repo or self.main), '--json'] + list(options))
        return code, out.getvalue(), err.getvalue()

    def proposed(self):
        code, out, err = self.invoke('--goal', 'Fix 1 typo in README.md')
        self.assertEqual((code, err), (0, ''), out)
        return json.loads(out)

    def model_calls(self):
        return [v for v in fsio.read_jsonl(self.calls) if v['model_call']] if self.calls.exists() else []

    def snapshot(self):
        return {name: (self.state / name).read_bytes() if (self.state / name).exists() else None
                for name in ('plan.json', 'plan.md', 'decisions.jsonl', 'project-map.json', 'form.json')}

    def test_prose_preamble_around_one_json_fence_plans_and_brief_forbids_prose(self):
        self.final.write_text("I've read the brief; here is the plan.\n\n```json\n" +
                              json.dumps(manager(), indent=2) + '\n```\nThat is all.\n')
        result = self.proposed()
        self.assertEqual(result['status'], 'proposed')
        self.assertEqual([t['id'] for t in result['tasks']], ['T1'])
        self.assertEqual(len(self.model_calls()), 1)
        published = ''.join((self.state / name).read_text() for name in ('plan.json', 'plan.md'))
        self.assertNotIn('read the brief', published)
        self.assertNotIn('That is all', published)
        brief = next((self.home / 'state/runs').glob('*/brief-source.md')).read_text()
        self.assertIn('no text before or after it', brief)

    def test_plan_records_classifier_version_bound_by_digest(self):
        from orcexe import classify
        result = self.proposed()
        self.assertEqual(result['classifier'], classify.CLASSIFIER_VERSION)
        self.assertEqual(result['classifier'], 'lane.v2')
        self.assertEqual(fsio.read_json(self.state / 'plan.json')['classifier'], 'lane.v2')
        changed = dict(result, classifier='lane.v1')
        self.assertNotEqual(planning.content_sha256(changed), result['content_sha256'])

    def test_state_dir_inside_repository_is_reported_without_model_call_or_plan(self):
        for inside in (self.main / 'state-inside', self.main / 'deep/er/state', self.main):
            with self.subTest(state=inside.relative_to(self.main).as_posix() or '.'):
                self.env['ORCEXE_STATE_DIR'] = str(inside)
                before = self.snapshot()
                result = self.invoke('--goal', 'Fix 1 typo in README.md')
                self.assertEqual(result, (2, '', 'not_seen: state_dir_in_repository\n'))
                self.assertEqual(self.model_calls(), [])
                self.assertFalse((self.state / 'plan.json').exists())
                self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.main / 'state-inside').exists())
        self.assertFalse((self.main / 'deep').exists())
        self.assertEqual(planning.EXIT_REASONS['state_dir_in_repository'], exitcodes.USAGE)

    def test_fake_manager_single_call_and_access_snapshot_routes_and_determinism(self):
        (self.main / 'README.md').write_text('DIRTY')
        gather = route.gather_access
        with mock.patch.object(route, 'gather_access', wraps=gather) as snapshot:
            first = self.proposed()
        snapshot.assert_called_once()
        self.assertEqual(len(self.model_calls()), 1)
        call = self.model_calls()[0]
        self.assertFalse(Path(call['cwd']).exists())
        self.assertIn('-p', call['argv'])
        self.assertEqual(call['argv'][-6:], ['--permission-mode', 'plan', '--restricted',
                                             '--safe-mode', '--tools', 'Read,Glob,Grep'])
        self.assertIn('--strict-mcp-config', call['argv'])
        receipt = next((self.home / 'state/runs').glob('*/result.json'))
        self.assertEqual(fsio.read_json(receipt)['profile'], 'manager')
        self.assertEqual(first['tasks'][0]['route']['tool'], 'codex')
        self.assertEqual(first['status'], 'proposed')
        summary = (self.state / 'current-state.md').read_text()
        self.assertIn(first['plan_id'] + ' / proposed', summary)
        self.assertIn('T1: pending', summary)
        self.assertEqual(first['head'], self.head)
        self.assertEqual(first['tasks'][0]['lane'], 'fast')
        for name in ('plan.json', 'plan.md'):
            self.assertEqual((self.state / name).stat().st_mode & 0o777, 0o600)
        brief = next((self.home / 'state/runs').glob('*/brief-source.md')).read_text()
        for expected in ('UNTRUSTED DATA', 'risk_map', 'do_not_touch', 'conventions', 'commands',
                         'assumptions', 'decisions', 'orcexe.manager.plan.v1'):
            self.assertIn(expected, brief)
        self.assertNotIn(str(self.main), brief)
        self.assertNotIn('DIRTY', brief)
        second = self.proposed()
        self.assertEqual(first.pop('plan_id'), second.pop('plan_id'))
        self.assertEqual(first, second)
        self.assertEqual(fsio.read_json(self.state / 'project-map.json'), self.value)


    def test_questions_pending_no_model_or_probes_and_answer_recomputed_for_goal(self):
        with mock.patch.object(route, 'gather_access') as access, mock.patch.object(mapping, 'manager_run') as run:
            code, out, err = self.invoke('--goal', 'Improve things')
        self.assertEqual((code, out, err), (2, '', 'not_seen: questions_pending\n'))
        access.assert_not_called()
        run.assert_not_called()
        form = fsio.read_json(self.state / 'form.json')
        self.assertEqual((self.state / 'form.json').stat().st_mode & 0o777, 0o600)
        qid = form['questions'][0]['id']
        self.assertEqual(self.invoke('--form', form['form_id'], '--text', qid + '=One corrected typo', command='answer')[0], 0)
        code, out, err = self.invoke('--goal', 'Improve things')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(json.loads(out)['decisions'], [qid])
        self.assertEqual(json.loads(out)['assumptions'][0]['assumed'], 'One corrected typo')
        before = len(self.model_calls())
        self.assertEqual(self.invoke('--goal', 'Different goal')[2], 'not_seen: questions_pending\n')
        self.assertEqual(len(self.model_calls()), before)

    def test_missing_stale_map_without_model(self):
        with mock.patch.object(mapping, 'manager_run') as run:
            (self.state / 'project-map.json').unlink()
            self.assertEqual(self.invoke('--goal', 'tests pass')[2], 'not_seen: map_missing\n')
            self.value['intake_sha'] = 'old'
            self.write_map()
            self.assertEqual(self.invoke('--goal', 'tests pass')[2], 'not_seen: map_stale\n')
        run.assert_not_called()



    def test_real_plan_replayed_with_brace_map_succeeds(self):
        fixture = ROOT / 'tests/fixtures/planning'
        self.value = json.loads((fixture / 'T40c-project-map.json').read_text())
        # Track a realistic package census, not just the map's four risk rows.
        for name in ('cli.py', 'ledger.py', 'redact.py', 'worktree.py', 'gitutil.py', 'trust.py',
                     'envfilter.py', 'adapters/base.py', 'mapping.py', 'planning.py'):
            path = self.main / 'plugins/orcexe/src/orcexe' / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n')
        self.git_run(self.main, 'add', 'plugins')
        self.git_run(self.main, 'commit', '-qm', 'package census')
        self.head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        self.value['intake_sha'] = self.head
        self.write_map()
        self.final.write_bytes((fixture / 'T40c-real-plan-final.md').read_bytes())
        expected = json.loads(self.final.read_text())['tasks'][0]
        goal = expected['goal'] + ' so that tests pass'
        code, out, err = self.invoke('--goal', goal, command='questions')
        self.assertEqual((code, err), (0, ''))
        form = json.loads(out)
        self.assertEqual(form['questions'], [])
        self.assertEqual({a['reason'] for a in form['assumptions']}, {'deferred_to_plan'})
        self.assertEqual(len(form['assumptions']), len(self.value['risk_map']) + len(self.value['do_not_touch']))
        self.assertEqual(self.model_calls(), [])
        code, out, err = self.invoke('--goal', goal)
        self.assertEqual((code, err), (0, ''), out)
        result = json.loads(out)
        self.assertEqual(result['status'], 'proposed')
        self.assertEqual(len(result['tasks']), 1)
        self.assertEqual(result['tasks'][0]['lane'], 'standard')
        self.assertEqual(result['tasks'][0]['route']['lane'], 'standard')
        self.assertEqual(result['tasks'][0]['route']['tool'], 'codex')
        self.assertEqual(result['tasks'][0]['notes'], expected['notes'])
        self.assertEqual(len(self.model_calls()), 1)
        self.assertEqual(fsio.read_json(self.state / 'plan.json'), result)
        self.assertEqual(fsio.read_json(self.state / 'form.json'), form)



    def test_notes_round_trip_and_rejection_detail_does_not_echo_content(self):
        self.final.write_text(json.dumps(manager([task(notes='Use the existing helper; verify edge cases.')])))
        result = self.proposed()
        self.assertEqual(result['tasks'][0]['notes'], 'Use the existing helper; verify edge cases.')
        self.assertEqual(fsio.read_json(self.state / 'plan.json')['tasks'][0]['notes'], result['tasks'][0]['notes'])
        self.assertIn(result['tasks'][0]['notes'], (self.state / 'plan.md').read_text())
        self.assertEqual(self.invoke('--approve', result['plan_id'])[0], 0)
        self.assertEqual(fsio.read_json(self.state / 'plan.json')['tasks'][0]['notes'], result['tasks'][0]['notes'])
        before = self.snapshot()
        self.final.write_text(json.dumps(manager([task(goal='PRIVATE_CONTENT' + 'x' * 1370)])))
        self.assertEqual(self.invoke('--goal', 'tests pass'),
                         (2, '', 'not_seen: manager_output_invalid\ndetail: tasks[0].goal too_long (1385 > 300)\n'))
        self.assertEqual(self.snapshot(), before)




    def test_approval_paths_and_no_model(self):
        with mock.patch.object(mapping, 'manager_run') as run:
            self.assertEqual(self.invoke('--approve', 'missing')[2], 'not_seen: plan_missing\n')
            run.assert_not_called()
        result = self.proposed()
        before = self.snapshot()
        with mock.patch.object(route, 'gather_access') as access, mock.patch.object(mapping, 'manager_run') as run:
            self.assertEqual(self.invoke('--approve', 'wrong')[2], 'not_seen: plan_mismatch\n')
            self.assertEqual(self.snapshot(), before)
            code, out, err = self.invoke('--approve', result['plan_id'])
            self.assertEqual((code, err), (0, ''))
            approved = json.loads(out)
            self.assertEqual(approved['status'], 'approved')
            self.assertIn('approved_at', approved)
            access.assert_not_called()
            run.assert_not_called()
            saved = self.snapshot()
            self.assertEqual(self.invoke('--approve', result['plan_id'])[2], 'not_seen: plan_not_proposed\n')
            self.assertEqual(self.snapshot(), saved)
        records = fsio.read_jsonl(self.state / 'decisions.jsonl')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]['kind'], 'plan_approval')
        self.assertEqual(records[0]['plan_id'], result['plan_id'])
        self.assertEqual(records[0]['schema'], 'orcexe.decision.v1')
        self.assertEqual((self.state / 'decisions.jsonl').stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.invoke('--goal', 'tests pass', command='questions')[0], 0)
        fresh = self.proposed()
        self.git_run(self.main, 'commit', '--allow-empty', '-qm', 'new head')
        saved = self.snapshot()
        self.assertEqual(self.invoke('--approve', fresh['plan_id'])[2], 'not_seen: plan_stale\n')
        self.assertEqual(self.snapshot(), saved)







    def unsupported_plan(self):
        self.final.write_text(json.dumps(manager([
            task(), task('T2', lane='risky'), task('T3', depends_on=['T2'])])))
        return self.proposed()

    # T60: risky is runnable (K18 a); this T59 test keeps lane_not_supported via the K18 (b) fallback.
    @mock.patch.object(planning, 'SUPPORTED_LANES', ('fast', 'standard'))
    def test_unsupported_lane_is_marked_in_json_and_markdown(self):
        result = self.unsupported_plan()
        self.assertEqual([t['lane'] for t in result['tasks']], ['fast', 'risky', 'fast'])
        self.assertEqual([t.get('lane_note') for t in result['tasks']], [None, 'lane_not_supported', None])
        self.assertEqual(result['status'], 'proposed')
        saved = fsio.read_json(self.state / 'plan.json')
        self.assertEqual(saved['tasks'][1]['lane_note'], 'lane_not_supported')
        markdown = (self.state / 'plan.md').read_text()
        row = next(line for line in markdown.splitlines() if line.startswith('| T2 |'))
        self.assertIn('| risky (lane_not_supported) |', row)
        self.assertNotIn('lane_not_supported', next(line for line in markdown.splitlines()
                                                     if line.startswith('| T1 |')))
        # T59 §2.6: the form shows what an approval runs and what it defers (was a refusal note).
        self.assertIn('## Will run after approval\n\nT1 (fast)\n', markdown)
        self.assertIn('## Will be deferred\n\n- T2 (risky): lane_not_supported — lane: risky', markdown)
        self.assertIn('- T3: dependency_deferred (depends on T2)', markdown)
        self.assertIn('Approve: `orcexe plan --approve ' + result['plan_id'] + '`', markdown)
        code, human, error = self.invoke('--goal', 'Fix 1 typo in README.md')
        self.assertEqual((code, error), (0, ''))
        self.assertEqual(json.loads(human)['tasks'][1]['lane_note'], 'lane_not_supported')
        plain = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(plain), redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['plan', '--repo', str(self.main), '--goal', 'Fix 1 typo in README.md']), 0)
        self.assertIn('risky (lane_not_supported)', plain.getvalue())
        # A supported-only plan carries no note anywhere.
        self.final.write_text(json.dumps(manager()))
        plain_plan = self.proposed()
        self.assertNotIn('lane_note', plain_plan['tasks'][0])
        self.assertNotIn('lane_not_supported', (self.state / 'plan.md').read_text())

    # T60: risky is runnable (K18 a); this T59 test keeps lane_not_supported via the K18 (b) fallback.
    @mock.patch.object(planning, 'SUPPORTED_LANES', ('fast', 'standard'))
    def test_approve_defers_unsupported_lane_and_refuses_only_when_nothing_runs(self):
        # T59 (was: approval refused the whole plan with lane_not_supported).
        from orcexe import ledger, task as task_module
        result = self.unsupported_plan()
        with mock.patch.object(mapping, 'manager_run') as run:
            code, out, error = self.invoke('--approve', result['plan_id'])
            run.assert_not_called()
        self.assertEqual((code, error), (0, ''))
        approved = json.loads(out)
        self.assertEqual((approved['plan_id'], approved['content_sha256']), (result['plan_id'], result['content_sha256']))
        self.assertEqual(approved['approval']['approved'], ['T1'])
        self.assertEqual(approved['approval']['deferred'], [
            dict(id='T2', reason='lane_not_supported', cause=None, evidence='lane: risky (this version runs fast and standard)'),
            dict(id='T3', reason='dependency_deferred', cause='T2', evidence='depends on T2')])
        self.assertIn('Status: approved (1 of 3; 2 deferred)', (self.state / 'plan.md').read_text())
        # Nothing runnable: plan_rejected names every task, writes nothing, plan stays proposed.
        self.final.write_text(json.dumps(manager([task('T1', lane='risky'), task('T2', depends_on=['T1']),
                                                  task('T3', lane='risky')])))
        second = self.proposed()
        before = self.snapshot()
        log_before = ledger.default_path(self.main).read_bytes()
        self.assertEqual(self.invoke('--approve', second['plan_id']), (2, '', 'not_seen: plan_rejected\n'
            'deferred: T1 lane_not_supported (lane: risky (this version runs fast and standard))\n'
            'deferred: T2 dependency_deferred (depends on T1)\n'
            'deferred: T3 lane_not_supported (lane: risky (this version runs fast and standard))\n'))
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(ledger.default_path(self.main).read_bytes(), log_before)
        self.assertEqual(fsio.read_json(self.state / 'plan.json')['status'], 'proposed')
        with mock.patch.object(task_module, 'run_task') as dispatch:
            code, out, error = self.invoke('--run', second['plan_id'])
            dispatch.assert_not_called()
        self.assertEqual(code, 2, (out, error))
        self.assertIn('not_seen: plan_not_approved', error)
        self.assertEqual(self.snapshot(), before)

    # T60: risky is runnable (K18 a); this T59 test keeps lane_not_supported via the K18 (b) fallback.
    @mock.patch.object(planning, 'SUPPORTED_LANES', ('fast', 'standard'))
    def test_lane_note_is_bound_and_legacy_unmarked_plans_still_refuse(self):
        original = self.unsupported_plan()
        for change, expected in [(lambda p: p['tasks'][0].update(lane_note='lane_not_supported'), 'plan_tampered'),
                                 (lambda p: p['tasks'][1].update(lane_note='other'), 'plan_tampered'),
                                 (lambda p: p['tasks'][1].pop('lane_note'), None)]:
            plan = copy.deepcopy(original)
            change(plan)
            plan['content_sha256'] = planning.content_sha256(plan)
            plan['plan_id'] = planning.plan_id(plan['content_sha256'])
            (self.state / 'plan.json').write_text(json.dumps(plan))
            before = self.snapshot()
            code, out, error = self.invoke('--approve', plan['plan_id'])
            if expected is None:
                # T59: an unmarked legacy risky task is still never approved to run; it is deferred.
                self.assertEqual((code, error), (0, ''))
                self.assertEqual([d['id'] for d in json.loads(out)['approval']['deferred']], ['T2', 'T3'])
                continue
            self.assertEqual((code, error.splitlines()[0]), (2, 'not_seen: ' + expected))
            self.assertEqual(self.snapshot(), before)

    def test_signal_uses_map_cleanup(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers = {s: signal.getsignal(s) for s in (signal.SIGINT, signal.SIGTERM)}
            def cancel(*args):
                os.kill(os.getpid(), sig)
            with mock.patch.object(mapping, 'manager_run', side_effect=cancel):
                self.assertEqual(self.invoke('--goal', 'tests pass')[0], 128 + sig)
            self.assertEqual({s: signal.getsignal(s) for s in handlers}, handlers)
            self.assertFalse((self.state / 'plan.json').exists())

    def test_head_movement_refuses_publication(self):
        def move(*args):
            self.git_run(self.main, 'commit', '--allow-empty', '-qm', 'moved')
            return json.dumps(manager()), {}
        with mock.patch.object(mapping, 'manager_run', side_effect=move):
            self.assertEqual(self.invoke('--goal', 'tests pass'), (2, '', 'not_seen: head_moved\n'))
        self.assertFalse((self.state / 'plan.json').exists())


    def test_symlink_guards_preserve_foreign_targets(self):
        foreign = self.directory / 'foreign'
        foreign.write_text('do not change')
        (self.state / 'plan.md').symlink_to(foreign)
        self.assertEqual(self.invoke('--goal', 'tests pass'), (2, '', 'not_seen: output_symlink\n'))
        self.assertEqual(foreign.read_text(), 'do not change')
        self.assertFalse((self.state / 'plan.json').exists())


    def test_reused_publisher_rolls_back_at_each_replace(self):
        old = dict(schema='orcexe.plan.v1', plan_id='old', head=self.head, goal='old goal',
                   status='proposed', decisions=[], assumptions=[], tasks=[])
        new = dict(old, plan_id='new')
        replace = os.replace
        for existing in (False, True):
            for approve in (False, True):
                for fail_at in range(1, 11 + int(approve)):
                    directory = self.directory / ('publish-{}-{}-{}'.format(existing, approve, fail_at))
                    directory.mkdir()
                    if existing:
                        planning.publish(directory, old)
                    if approve:
                        fsio.append_jsonl(directory / 'decisions.jsonl', {'keep': True})
                    names = ['plan.json', 'plan.md', 'decisions.jsonl']
                    before = {name: (directory / name).read_bytes() if (directory / name).exists() else None for name in names}
                    count = 0
                    def fail(source, target):
                        nonlocal count
                        count += 1
                        if count == fail_at:
                            raise OSError('injected')
                        replace(source, target)
                    with self.subTest(existing=existing, approve=approve, fail_at=fail_at):
                        with mock.patch.object(mapping.os, 'replace', side_effect=fail):
                            try:
                                planning.publish(directory, new, {'new': True} if approve else None)
                            except OSError:
                                after = {name: (directory / name).read_bytes() if (directory / name).exists() else None for name in names}
                                self.assertEqual(after, before)
                                continue
                        self.assertGreater(fail_at, count)







