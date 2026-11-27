"""One L0 policy pass per case; full three-attempt process matrices are slow."""

import inspect
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
from unittest import mock

from tests.security import PrivateHomes
from tests.unit.test_task import APP, TEST
from orcexe import gate_lite, task_l0


class FastL0PolicyTests(PrivateHomes):
    def test_baseline_overlay_counts_skips_and_doc_exemption(self):
        # These cases preserve default coverage of the longer L0 task scenarios.
        for case in ('failing-base', 'doc-only', 'new-test', 'old-skip', 'static-drop', 'runtime-skip', 'new-symbol'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                run_dir = root / 'run'
                (run_dir / 'gate').mkdir(parents=True)
                before = {'src/app.py': APP, 'tests/test_app.py': TEST}
                after = dict(before)
                changed = 'tests/test_app.py'
                after[changed] = TEST.replace('1', '2')
                if case == 'doc-only':
                    changed = 'README.md'
                    before[changed], after[changed] = 'Before\n', 'After\n'
                    after['tests/test_app.py'] = TEST
                elif case == 'new-test':
                    changed = 'tests/test_new.py'
                    after[changed] = TEST.replace('1', '2')
                elif case == 'new-symbol':
                    after[changed] = TEST + '\nfrom src.app import new_symbol\n'
                elif case == 'old-skip':
                    before[changed] = TEST.replace('    def test_app', '    @unittest.' + 'skip(\"old\")\n    def test_app')
                    after[changed] = before[changed] + '\n# preserved marker\n'
                elif case == 'static-drop':
                    before[changed] += '\ndef test_extra():\n    assert True\n'
                old = before.get(changed, '')
                new = after[changed]
                file = dict(path=changed, old_path=changed if old else None,
                            new_mode='100644', old_mode='100644' if old else '000000')
                observed = dict(tree='candidate', versions=[(file, ('old', 'new'))], files=[file])
                runtime_count = 50 if case == 'static-drop' else len([p for p in after if task_l0.is_test(p)])
                runtime = 'Ran {} tests in 0.01s\nOK{}\n'.format(runtime_count,
                            ' (skipped=1)' if case == 'runtime-skip' else '')
                # T55: L0 reads the gate's in-process log copy, as the gate writes it.
                gate_lite._write_log(run_dir / 'gate', 'unittest', runtime)
                phases = []

                def checkout(repo, tree, scratch, *args):
                    copy = Path(scratch) / 'checkout'
                    copy.mkdir()
                    for name, text in (after if tree == 'candidate' else before).items():
                        path = copy / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text)
                    return copy

                def observe(commands, checkout, directory, *args):
                    phases.append(directory.name)
                    if directory.name == 'red':
                        self.assertEqual((checkout / changed).read_text(), new)
                        self.assertEqual((checkout / 'src/app.py').read_text(), APP)
                    return [dict(count=len([p for p in before if task_l0.is_test(p)]),
                                 skip_counts=dict(skipped=0, xfailed=0, expected_failures=0),
                                 exit_code=1 if directory.name == 'red' or case == 'failing-base' else 0)]

                with mock.patch.dict(os.environ, ORCEXE_STATE_DIR=str(root / 'state')), \
                        mock.patch.object(gate_lite, '_checkout', side_effect=checkout), \
                        mock.patch.object(gate_lite, '_blobs', return_value={'old': old.encode(), 'new': new.encode()}), \
                        mock.patch.object(gate_lite, '_collect', return_value=([], [file], [(':100644 100644 old new M', changed, changed)])), \
                        mock.patch.object(gate_lite, 'project_python', return_value=dict(declared=None, path=None)), \
                        mock.patch.object(task_l0, '_tests', side_effect=observe):
                    result = task_l0.run_l0(repo_path=root, worktree=SimpleNamespace(path=str(root), base_commit='base'),
                        observed=observed, commands=[gate_lite.Command('unittest', ['python3', '-m', 'unittest'])],
                        gate={}, run_dir=run_dir)
                checks = {c['id']: c['status'] for c in result['checks']}
                self.assertEqual(phases, ['base'] if case == 'doc-only' else ['base', 'red'])
                self.assertEqual(checks['test_count_dropped'], 'fail' if case == 'static-drop' else 'pass')
                self.assertEqual(checks['skip_added'], 'fail' if case == 'runtime-skip' else 'pass')
                self.assertEqual(checks['tests_missing'], 'pass')
                # T54c: a docs-only diff passes red-first (was 'fail', which blocked every docs-only task).
                self.assertEqual(checks['red_first_missing'], 'pass')
                if case == 'doc-only':
                    self.assertEqual({c['id']: c['detail'] for c in result['checks']}['red_first_missing'], 'docs_only')
                    self.assertEqual(sorted(checks), sorted(task_l0.CHECK_IDS))

    def test_docs_only_red_first_exemption_is_narrow(self):
        # Every changed path must be a doc; a code or test file keeps the red-first proof.
        for case, edits, red_exit in (
                ('docs-only', {'README.md': 'After\n', 'CHANGELOG.md': 'After\n', 'skill/SKILL.md': 'After\n'}, None),
                ('doc-and-code', {'README.md': 'After\n', 'src/app.py': APP + '\n'}, None),
                ('doc-and-test', {'README.md': 'After\n', 'tests/test_app.py': TEST + '\n'}, 0)):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                run_dir = root / 'run'
                (run_dir / 'gate').mkdir(parents=True)
                before = {'src/app.py': APP, 'tests/test_app.py': TEST, 'README.md': 'Before\n',
                          'CHANGELOG.md': 'Before\n', 'skill/SKILL.md': 'Before\n'}
                after = dict(before, **edits)
                files, versions, fingerprints, blobs = [], [], [], {}
                for index, name in enumerate(sorted(edits)):
                    file = dict(path=name, old_path=name, new_mode='100644', old_mode='100644')
                    old, new = 'old%d' % index, 'new%d' % index
                    blobs[old], blobs[new] = before[name].encode(), after[name].encode()
                    files.append(file)
                    versions.append((file, (old, new)))
                    fingerprints.append((':100644 100644 %s %s M' % (old, new), name, name))
                observed = dict(tree='candidate', versions=versions, files=files)
                count = len([p for p in after if task_l0.is_test(p)])
                # T55: L0 reads the gate's in-process log copy, as the gate writes it.
                gate_lite._write_log(run_dir / 'gate', 'unittest', 'Ran {} tests in 0.01s\nOK\n'.format(count))
                phases = []

                def checkout(repo, tree, scratch, *args):
                    copy = Path(scratch) / 'checkout'
                    copy.mkdir()
                    for name, text in (after if tree == 'candidate' else before).items():
                        path = copy / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text)
                    return copy

                def observe(commands, checkout, directory, *args):
                    phases.append(directory.name)
                    return [dict(count=count, skip_counts=dict(skipped=0, xfailed=0, expected_failures=0),
                                 exit_code=red_exit if directory.name == 'red' else 0)]

                with mock.patch.dict(os.environ, ORCEXE_STATE_DIR=str(root / 'state')), \
                        mock.patch.object(gate_lite, '_checkout', side_effect=checkout), \
                        mock.patch.object(gate_lite, '_blobs', return_value=blobs), \
                        mock.patch.object(gate_lite, '_collect', return_value=([], files, fingerprints)), \
                        mock.patch.object(gate_lite, 'project_python', return_value=dict(declared=None, path=None)), \
                        mock.patch.object(task_l0, '_tests', side_effect=observe):
                    result = task_l0.run_l0(repo_path=root, worktree=SimpleNamespace(path=str(root), base_commit='base'),
                        observed=observed, commands=[gate_lite.Command('unittest', ['python3', '-m', 'unittest'])],
                        gate={}, run_dir=run_dir)
                checks = {c['id']: (c['status'], c['detail']) for c in result['checks']}
                self.assertEqual(sorted(checks), sorted(task_l0.CHECK_IDS))
                if case == 'docs-only':
                    self.assertTrue(result['passed'], result)
                    self.assertEqual(checks['red_first_missing'], ('pass', 'docs_only'))
                    self.assertEqual(phases, ['base'])
                elif case == 'doc-and-code':
                    self.assertFalse(result['passed'])
                    self.assertEqual(checks['tests_missing'][0], 'fail')
                    self.assertEqual(checks['red_first_missing'], ('fail', 'no_observed_failure_on_base'))
                else:
                    self.assertFalse(result['passed'])
                    self.assertEqual(checks['tests_missing'][0], 'pass')
                    self.assertEqual(checks['red_first_missing'], ('fail', 'no_observed_failure_on_base'))
                    self.assertEqual(phases, ['base', 'red'])

    def test_tests_only_red_first_exemption_preserves_other_checks(self):
        helper = 'def value():\n    return 1\n'
        test = ('import unittest\nfrom tests._fixtures import value\n'
                'class FixtureTest(unittest.TestCase):\n'
                '    def test_value(self):\n        self.assertEqual(value(), 1)\n')
        for case, red_exit in (('tests-only', 0), ('mixed', 0), ('hollow', 0), ('skip', 0),
                               ('tests-only-red', 1), ('tests-only-unknown', None),
                               ('mixed-red', 1), ('mixed-unknown', None), ('unsafe-overlay', 0)):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                run_dir = root / 'run'
                (run_dir / 'gate').mkdir(parents=True)
                before = {'src/app.py': APP, 'tests/test_app.py': TEST}
                edits = {'tests/_fixtures.py': helper, 'tests/test_fixtures.py': test}
                if case.startswith('mixed'):
                    edits['src/app.py'] = APP + '\n'
                elif case == 'hollow':
                    edits['tests/test_fixtures.py'] = test.replace('self.assertEqual(value(), 1)', 'return')
                elif case == 'skip':
                    edits['tests/test_fixtures.py'] = test.replace(
                        '    def test_value', '    @unittest.' + 'skip("reason")\n    def test_value')
                after = dict(before, **edits)
                files, versions, fingerprints, blobs = [], [], [], {}
                for index, name in enumerate(sorted(edits)):
                    existing = name in before
                    file = dict(path=name, old_path=name if existing else None, new_mode='100644',
                                old_mode='100644' if existing else '000000')
                    old, new = 'old%d' % index, 'new%d' % index
                    blobs[old], blobs[new] = before.get(name, '').encode(), after[name].encode()
                    files.append(file)
                    versions.append((file, (old, new)))
                    fingerprints.append((':%s 100644 %s %s %s' % (
                        file['old_mode'], old, new, 'M' if existing else 'A'), name, file['old_path']))
                observed = dict(tree='candidate', versions=versions, files=files)
                gate_lite._write_log(run_dir / 'gate', 'unittest', 'Ran 2 tests in 0.01s\nOK\n')
                phases = []

                def checkout(repo, tree, scratch, *args):
                    copy = Path(scratch) / 'checkout'
                    copy.mkdir()
                    for name, text in (after if tree == 'candidate' else before).items():
                        path = copy / name
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(text)
                    return copy

                def observe(commands, checkout, directory, *args):
                    phases.append(directory.name)
                    self.assertEqual((checkout / 'src/app.py').read_text(), APP)
                    if directory.name == 'red':
                        for name in ('tests/_fixtures.py', 'tests/test_fixtures.py'):
                            self.assertEqual((checkout / name).read_text(), edits[name])
                    else:
                        self.assertFalse((checkout / 'tests/_fixtures.py').exists())
                    return [dict(count=2 if directory.name == 'red' else 1,
                                 skip_counts=dict(skipped=0, xfailed=0, expected_failures=0),
                                 exit_code=red_exit if directory.name == 'red' else 0)]

                inside_copy = gate_lite._inside_copy
                with mock.patch.dict(os.environ, ORCEXE_STATE_DIR=str(root / 'state')), \
                        mock.patch.object(gate_lite, '_checkout', side_effect=checkout), \
                        mock.patch.object(gate_lite, '_blobs', return_value=blobs), \
                        mock.patch.object(gate_lite, '_collect', return_value=([], files, fingerprints)), \
                        mock.patch.object(gate_lite, 'project_python', return_value=dict(declared=None, path=None)), \
                        mock.patch.object(gate_lite, '_inside_copy', side_effect=(
                            lambda *args: False if case == 'unsafe-overlay' else inside_copy(*args))), \
                        mock.patch.object(task_l0, '_tests', side_effect=observe):
                    result = task_l0.run_l0(repo_path=root, worktree=SimpleNamespace(path=str(root), base_commit='base'),
                        observed=observed, commands=[gate_lite.Command('unittest', ['python3', '-m', 'unittest'])],
                        gate={}, run_dir=run_dir)
                checks = {c['id']: (c['status'], c['detail']) for c in result['checks']}
                self.assertEqual(sorted(checks), sorted(task_l0.CHECK_IDS))
                self.assertEqual(phases, ['base'] if case == 'unsafe-overlay' else ['base', 'red'])
                self.assertEqual(checks['test_count_dropped'], ('pass', 'base=[1]; candidate=[2]'))
                self.assertEqual(checks['tests_missing'], ('pass', 'changed_tests=2'))
                self.assertEqual(checks['skip_added'][0], 'fail' if case in ('hollow', 'skip') else 'pass')
                self.assertEqual(checks['assertion_missing'][0], 'fail' if case == 'hollow' else 'pass')
                if case == 'unsafe-overlay':
                    expected = ('not_seen', 'observation_unavailable')
                elif case.startswith('mixed') or red_exit:
                    if red_exit:
                        expected = ('pass', 'tests_fail_on_base')
                    else:
                        expected = ('not_seen' if red_exit is None else 'fail', 'no_observed_failure_on_base')
                else:
                    expected = ('pass', 'tests_only')
                self.assertEqual(checks['red_first_missing'], expected)
                self.assertEqual(result['passed'], case in (
                    'tests-only', 'tests-only-red', 'tests-only-unknown', 'mixed-red'))

    def test_tests_only_predicate_is_not_a_hollow_test(self):
        source = inspect.getsource(task_l0.tests_only).encode()
        observed = dict(versions=[(dict(path='plugins/orcexe/src/orcexe/task_l0.py'), ('old', 'new'))])
        self.assertEqual(task_l0._skip_added(observed, dict(old=b'', new=source)), [])

    def test_tests_only_predicate(self):
        def file(path, old_path=None, new_mode='100644'):
            return dict(path=path, old_path=old_path, new_mode=new_mode)

        for case, files, expected in (
                ('empty', [], False),
                ('added-test', [file('tests/test_new.py')], True),
                ('added-helper', [file('tests/_fixtures.py')], True),
                ('modified-test', [file('tests/test_app.py', 'tests/test_app.py')], True),
                ('test-rename', [file('tests/test_new.py', 'tests/test_old.py')], True),
                ('product-to-test', [file('tests/test_app.py', 'src/app.py')], False),
                ('test-to-product', [file('src/app.py', 'tests/test_app.py')], False),
                ('doc-to-test', [file('tests/test_app.py', 'README.md')], False),
                ('test-to-doc', [file('README.md', 'tests/test_app.py')], False),
                ('product-and-test', [file('tests/test_app.py'), file('src/app.py')], False),
                ('doc-and-test', [file('tests/test_app.py'), file('README.md')], False),
                ('test-directory-doc', [file('tests/test_app.py'), file('tests/README.md')], False),
                ('deleted-only', [file('tests/test_app.py', 'tests/test_app.py', '000000')], False),
                ('deleted-and-added-tests', [file('tests/test_old.py', 'tests/test_old.py', '000000'),
                                            file('tests/test_new.py', new_mode='100755')], True)):
            with self.subTest(case=case):
                self.assertEqual(task_l0.tests_only(files), expected)

    def test_docs_only_predicate(self):
        self.assertTrue(task_l0.docs_only({'README.md', 'CHANGELOG.md', 'docs/x.rst', 'LICENSE'}))
        self.assertFalse(task_l0.docs_only(set()))
        self.assertFalse(task_l0.docs_only({'README.md', 'src/app.py'}))
        self.assertFalse(task_l0.docs_only({'README.md', 'tests/test_app.py'}))
