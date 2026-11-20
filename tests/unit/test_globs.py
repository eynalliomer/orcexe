"""One bounded grammar for mapping, relevance, planning and future paths."""
import unittest

from orcexe import globs, mapping, planning, questions


class SharedGlobTests(unittest.TestCase):
    def test_shared_validity_and_match_overlap_agree(self):
        cases = [
            ('plugins/orcexe/src/orcexe/{worktree,gitutil}.py',
             'plugins/orcexe/src/orcexe/gitutil.py'),
            ('plugins/orcexe/src/orcexe/{envfilter,adapters/*}.py',
             'plugins/orcexe/src/orcexe/adapters/base.py'),
            ('{src/a?,lib/**}/[!a-c]*.py', 'lib/deep/z.py'),
            ('src/**/{app,other}.py', 'src/app.py'),
            ('{src,lib}/[a-c]?.py', 'lib/ab.py'),
            ('[{,}].py', ',.py'), ('[**].py', '*.py'),
            ('.orcexe/**', '.orcexe/state.json'), ('.git/**', '.git/config'),
            ('vendor/**', 'vendor/lib.py'), ('.env*', '.env.example'),
        ]
        for pattern, path in cases:
            with self.subTest(pattern=pattern):
                self.assertTrue(globs.valid(pattern))
                self.assertTrue(mapping.valid_item_path(pattern, glob=True))
                self.assertEqual(planning.expand(pattern), globs.expand(pattern))
                self.assertTrue(globs.matches(path, pattern))
                self.assertTrue(mapping.covers_hotspot(path, pattern))
                # Literal paths for overlap must themselves escape glob metacharacters.
                if '*' not in path:
                    self.assertTrue(planning.overlaps(pattern, path))
        for pattern in ('', '/x', 'a/../b', 'C:/x', 'a\\b', 'a\n', 'a//b',
                        'a**b', '[z-a]', '[]', 'x]', '{a,b', '{a}', '{a,}',
                        '{a,{b,c}}', '{src,/tmp}/a', '{a,../b}', '{a,b}' * 9,
                        '{' + ','.join(['a'] * 1000) + '}'):
            with self.subTest(pattern=pattern):
                self.assertFalse(globs.valid(pattern))
                self.assertFalse(mapping.valid_item_path(pattern, glob=True))
                self.assertIn(('invalid_glob', 'T1'), planning.checks([
                    dict(id='T1', done=['done'], size='S', depends_on=[], parallel=False,
                         files={'allow': [pattern]})]))

    def test_expansion_and_work_limits_are_deterministic(self):
        self.assertEqual(len(globs.expand('{a,b}' * 8)), globs.EXPANSION_CAP)
        with self.assertRaises(ValueError):
            globs.expand('{a,b}' * 9)
        with self.assertRaises(ValueError):
            globs.expand('x' * 60 + '{a,b}' * 6)
        pattern = '{a,b}' * 8
        self.assertTrue(globs.valid(pattern))
        with self.assertRaisesRegex(ValueError, 'glob_work_limit'):
            globs.overlaps(pattern, 'z' * 1000)
        self.assertEqual(globs.hotspot_coverage('src/**', ['src/a', 'other/b'], [10000]), {'src/a'})
        self.assertEqual(globs.hotspot_coverage('src/**', ['src/a', 'src/b', 'other/c'], [10000]), set())

    def test_goal_risk_confirmations_are_deferred_even_with_brace_paths(self):
        project = {'risk_map': [dict(path_glob='{src/redact*,lib/adapters/*}.py', **{'class': 'data'})]}
        form = questions.select(project, 'head', 'Update adapters so that tests pass',
                                tracked_paths=['src/redact.py', 'lib/adapters/base.py', 'README.md'])
        self.assertEqual(form['questions'], [])
        self.assertEqual([a['reason'] for a in form['assumptions']], ['deferred_to_plan'])
        self.assertEqual([q['kind'] for q in questions.select(project, 'head')['questions']], ['risk_confirm'])

    def test_case_nfd_directory_risk_and_overlap_are_conservative(self):
        from orcexe import classify
        from tests.unit.test_planning import task
        project = {'risk_map': [dict(path_glob='shop/Étage.py', **{'class': 'payment'})],
                   'do_not_touch': [dict(path_glob='vendor')]}
        for path in ('Shop/E\u0301TAGE.py', 'vendor/hidden.py', 'VENDOR/hidden.py'):
            self.assertEqual(planning.task_lane(task(files={'allow': [path]}), project)[0], 'risky')
        for path in ('src/AUTH/new.py', 'db/Migrations/new.py', '.GitHub/config.yml',
                     'dockerfile', '.ENV', 'server.PEM', 'main.TF'):
            self.assertEqual(planning.task_lane(task(files={'allow': [path]}), {})[0], 'risky', path)
            self.assertEqual(classify.classify('Fix ' + path, [path]).lane, 'risky', path)
        for left, right in [('README.md', 'readme.md'), ('Étage/a.py', 'e\u0301tage/A.py')]:
            self.assertIn(('parallel_overlap', 'T1'), planning.checks([
                task(parallel=True, files={'allow': [left]}),
                task('T2', parallel=True, files={'allow': [right]})]))
            self.assertTrue(globs.matches(right, left))
        for pattern in ('src', 'src/', 'SRC/'):
            self.assertTrue(globs.valid(pattern))
            self.assertTrue(globs.matches('src/deep/future.py', pattern))
            self.assertTrue(globs.overlaps(pattern, 'src/deep/future.py'))
            self.assertEqual(globs.hotspot_coverage(pattern, ['src/a.py', 'other/b.py'], [10000]), {'src/a.py'})
        self.assertFalse(globs.overlaps('vendor', 'vendorish/file.py'))

    def test_local_directory_names_with_dots_are_scopes(self):
        import tempfile
        from pathlib import Path
        from tests.unit.test_planning import task
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'assets.cache').mkdir()
            directories = globs.directory_keys(['ASSETS.CACHE', '**/*.png'], root)
            self.assertTrue(globs.overlaps('ASSETS.CACHE', '**/*.png', directories))
            self.assertIn(('parallel_overlap', 'T1'), planning.checks([
                task(parallel=True, files={'allow': ['ASSETS.CACHE']}),
                task('T2', parallel=True, files={'allow': ['**/*.png']})], directories))
