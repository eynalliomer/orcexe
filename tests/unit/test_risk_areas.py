"""Risk policy agrees across planning, changed paths and escalation."""

from unittest import mock

from tests.unit.test_gitutil import GitFixture
from tests.unit.test_planning import task as plan_task
from orcexe import classify, fsio, planning, risk_areas, task


class RiskAreaTests(GitFixture):
    def setUp(self):
        super().setUp()
        (self.main / 'tests').mkdir()
        (self.main / 'tests/test_app.py').write_text('pass\n')
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '-qm', 'seed')

    def lane(self, allowed, project=None):
        with mock.patch.dict('os.environ', self.env, clear=True):
            return planning.task_lane(plan_task(goal='Implement a helper', lane='standard',
                                                files={'allow': allowed}), project or {}, self.main)

    def test_directory_glob_needs_literal_or_tracked_risk(self):
        for pattern in ('tests/**', 'tests/*.py', 'tests'):
            self.assertEqual(self.lane([pattern])[0], 'standard', pattern)
        (self.main / 'tests/auth').mkdir()
        path = self.main / 'tests/auth/test_flow.py'
        path.write_text('pass\n')
        # Untracked files alone do not change plan lanes.
        self.assertEqual(self.lane(['tests/**'])[0], 'standard')
        self.git_run(self.main, 'add', 'tests/auth/test_flow.py')
        self.git_run(self.main, 'commit', '-qm', 'tracked risk')
        for pattern in ('tests/**', 'tests'):
            lane, basis = self.lane([pattern])
            self.assertEqual(lane, 'risky')
            self.assertIn('cli:risk_path:**/*auth*', basis)
        self.assertEqual(self.lane(['tests/*.py'])[0], 'standard')

    def test_literal_risk_and_named_project_overlap_keep_basis(self):
        for pattern in ('src/auth/*.py', 'tests/test_payment.py', 'db/migrations/**', '.github/**'):
            self.assertEqual(self.lane([pattern])[0], 'risky', pattern)
        for section, entry, basis in (
            ('risk_map', dict(path_glob='shop/pay.py', **{'class': 'payment'}),
             'cli:risk_map:payment:shop/pay.py'),
            ('do_not_touch', dict(path_glob='shop/private/**'), 'cli:do_not_touch:shop/private/**')):
            lane, labels = self.lane(['shop/**'], {section: [entry]})
            self.assertEqual(lane, 'risky')
            self.assertIn(basis, labels)

    def test_all_sources_share_areas_and_concrete_path_inheritance(self):
        project = dict(risk_map=[dict(path_glob='shop/pay.py', **{'class': c})
                                 for c in ('auth', 'payment', 'data', 'migration')] +
                       [dict(path_glob='safe/**', **{'class': 'infra'})],
                       do_not_touch=[dict(path_glob='private')])
        fsio.atomic_write_json(self.main / '.orcexe/project-map.json', project)
        (self.main / '.orcexe/risk-map.yaml').write_text('version: 1\nrisky_paths: [local/**]\n')
        areas = risk_areas.repository_areas(self.main)
        self.assertIn(risk_areas.RiskArea('local/**', 'cli:risk_path:local/**'), areas)
        self.assertFalse(any(a.pattern == 'safe/**' for a in areas))
        for path in ('src/auth/new.py', 'shop/pay.py', 'private/deep/file.py', 'local/new.py'):
            with self.subTest(path=path), mock.patch.object(risk_areas, 'repository_areas', wraps=risk_areas.repository_areas) as shared:
                self.assertEqual(self.lane([path], project)[0], 'risky')
                self.assertEqual(task._escalation(self.main, [path])['suggested_lane'], 'risky')
                self.assertEqual(risk_areas.touched_paths(self.main, [path]), [path])
                self.assertEqual(shared.call_count, 3)
        self.assertEqual(task._escalation(self.main, ['safe/new.py'])['suggested_lane'], 'standard')
        self.assertEqual({a.pattern for a in areas if a.basis.startswith('cli:risk_path:')},
                         set(classify._RISK_PATHS) | {'local/**'})

    def test_invalid_inputs_never_grant_concrete_path_access_or_lower_lane(self):
        fsio.atomic_write_json(self.main / '.orcexe/project-map.json', {})
        local = self.main / '.orcexe/risk-map.yaml'
        local.write_text('invalid: true\n')
        self.assertEqual(self.lane(['tests/**'])[0], 'standard')
        self.assertEqual(self.lane(['src/auth/new.py'])[0], 'risky')
        self.assertEqual(task._escalation(self.main, ['safe.py'])['suggested_lane'], 'risky')
        local.unlink()
        (self.main / '.orcexe/project-map.json').write_text('{broken')
        self.assertEqual(task._escalation(self.main, ['safe.py'])['suggested_lane'], 'risky')
        self.assertEqual(risk_areas.touched_paths(self.main, ['safe.py']), ['safe.py'])
        for invalid in ({'risk_map': {}}, {'do_not_touch': None}, []):
            fsio.atomic_write_json(self.main / '.orcexe/project-map.json', invalid)
            self.assertEqual(risk_areas.touched_paths(self.main, ['safe.py']), ['safe.py'])
        with mock.patch.object(classify, 'load_risk_map', side_effect=PermissionError('denied')):
            self.assertEqual(risk_areas.touched_paths(self.main, ['safe.py']), ['safe.py'])
