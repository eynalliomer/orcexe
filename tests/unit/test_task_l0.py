"""L0 reports runner evidence and proves changed tests fail on pristine base."""

import json
from pathlib import Path
import unittest
from unittest import mock

from tests.unit.test_task import TaskFixture, APP, TEST
from tests.unit.test_planning import task as plan_task
from orcexe import gate_lite, task_l0


class CountTests(unittest.TestCase):
    def test_unittest_and_pytest_counts(self):
        for text, expected in [('Ran 12 tests in 0.01s\n\nOK', 12),
                               ('Ran 1 test in 1.23s\nFAILED (failures=1)', 1),
                               ('Ran 0 tests in 0.01s', 0),
                               ('...\n3 passed in 0.13s\n', 3),
                               ('=== 2 failed, 3 passed, 1 skipped, 2 xfailed, 1 xpassed, 4 deselected, 8 warnings in 1.20s ===', 9),
                               ('collected 14 items\nall good', None), ('0.3s complete', None),
                               ('no tests ran in 0.1s', None),
                               ('Ran 1 test in 0.01s\nOK\nRan 2 tests in 0.02s\nOK', 2)]:
            with self.subTest(text=text):
                self.assertEqual(task_l0.test_count(text), expected)

    def test_file_classification(self):
        for path in ('tests/test_app.py', 'test_app.py', 'testApp.py', 'app_test.py', 'app.test.ts', 'app.spec.jsx',
                     'src/__tests__/app.ts', 'test_app.mjs', 'app.test.cjs', 'src/app_test.go', 'AppTests.swift'):
            self.assertTrue(task_l0.is_test(path), path)
        for path in ('src/app.py', 'contest.py', 'docs/testing.md'):
            self.assertFalse(task_l0.is_test(path), path)
        self.assertTrue(task_l0.is_doc('README.md'))
        self.assertTrue(task_l0.is_doc('README.markdown'))
        self.assertTrue(task_l0.is_doc('docs/guide.rst'))
        self.assertFalse(task_l0.is_doc('src/app.py'))
        self.assertFalse(task_l0.is_doc('docs/conf.py'))


    def test_skip_detector_added_markers_and_unchanged_literals(self):
        cases = [
            ('tests/test_app.py', 'import unittest\n' + TEST,
             'import unittest\n' + TEST.replace('    def test_app', '    @unittest.skip("reason")\n    def test_app'), True),
            ('tests/test_app.py', TEST, TEST + '\n# @unittest.skip("example")\n', False),
            ('tests/app.js', "test('works', () => {});\n", "test.only('works', () => {});\n", True),
            ('tests/app.js', "test\n('works', () => {});\n", "test\n.only('works', () => {});\n", True),
            ('tests/app.js', "fit('old', () => {});\n", "// added comment\nfit('old', () => {});\n", False),
            ('tests/app.js', '', 'const text = "test.only( and xit(";\n', False),
            ('tests/test_app.py', TEST, 'import pytest\n' + TEST.replace('    def test_app',
              '    @pytest.mark.xfail(reason="edge")\n    def test_app'), True),
        ]
        for path, before, after, expected in cases:
            with self.subTest(path=path, after=after):
                observed = dict(versions=[(dict(path=path), ('old', 'new'))])
                result = task_l0._skip_added(observed, dict(old=before.encode(), new=after.encode()))
                self.assertEqual(bool(result), expected)

    def test_runtime_final_summary_and_skip_totals(self):
        text = 'Ran 50 tests in 0.1s\nOK\nRan 1 test in 0.01s\nOK (skipped=1, expected failures=2)\n'
        self.assertEqual(task_l0.runner_summary(text), dict(count=1, skips=3, skip_counts=dict(skipped=1, xfailed=0, expected_failures=2)))
        self.assertEqual(task_l0.runner_summary('50 passed in 1.0s\n1 passed, 2 xfailed in 0.1s'),
                         dict(count=3, skips=2, skip_counts=dict(skipped=0, xfailed=2, expected_failures=0)))
        self.assertEqual(task_l0.runner_summary('collected 5 items'), dict(count=None, skips=None, skip_counts=None))

    def test_each_runtime_skip_and_hollow_form(self):
        cases = ['raise unittest.SkipTest("reason")', 'pytest.skip("reason")',
                 'pytest.xfail("reason")', 'pytest.importorskip("missing")', 'return', 'pass', '...']
        for body in cases:
            after = 'def test_example():\n    ' + body + '\n'
            observed = dict(versions=[(dict(path='tests/test_example.py'), ('old', 'new'))])
            with self.subTest(body=body):
                self.assertTrue(task_l0._skip_added(observed, dict(old=b'', new=after.encode())))
        alias = 'from unittest import expectedFailure as marker\n@marker\ndef test_example():\n    assert True\n'
        self.assertTrue(task_l0._skip_added(observed, dict(old=b'', new=alias.encode())))
        for decorator in ('unittest.expectedFailure', 'pytest.mark.xfail'):
            after = '@' + decorator + '\ndef test_example():\n    assert True\n'
            self.assertTrue(task_l0._skip_added(observed, dict(old=b'', new=after.encode())))

    def test_missing_assertion_only_in_new_or_changed_test_body(self):
        before = 'def test_old():\n    helper()\n'
        for body, missing in [('pass', True), ('helper()', True), ('assert value', False),
                              ('self.assertEqual(value, 2)', False), ('self.fail("no")', False),
                              ('with pytest.raises(ValueError):\n        helper()', False),
                              ('def nested():\n        assert value', True)]:
            after = before + 'def test_new():\n    ' + body + '\n'
            observed = dict(versions=[(dict(path='tests/test_example.py'), ('old', 'new'))])
            findings = task_l0._assertion_missing(observed, dict(old=before.encode(), new=after.encode()))
            self.assertEqual(bool(findings), missing, body)
            self.assertFalse(any(f['line'] == 1 for f in findings))
        before = "test('old', () => { helper(); });\n"
        after = before + "test('new', () => { helper(); });\n"
        observed = dict(versions=[(dict(path='app.test.js'), ('old', 'new'))])
        findings = task_l0._assertion_missing(observed, dict(old=before.encode(), new=after.encode()))
        self.assertEqual([f['line'] for f in findings], [2])
        after = before + "test('new', () => { expect(value).toBe(2); });\n"
        self.assertEqual(task_l0._assertion_missing(observed, dict(old=before.encode(), new=after.encode())), [])
        after = "test('new', () => { return; expect(value).toBe(2); });\n"
        self.assertTrue(task_l0._skip_added(observed, dict(old=b'', new=after.encode())))

    def test_static_runner_conventions_ignore_comments_and_literals(self):
        import tempfile
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for name, text in {'test_unit.py': 'import unittest\nclass SomeCase(unittest.TestCase):\n    def testOne(self): pass\n',
                               'pytest_test.py': 'class TestThing:\n    def test_one(self): pass\ndef test_two(): pass\n',
                               'helper.py': 'def test_hidden(): pass\n',
                               'app.test.ts': 'test("one", () => {}); it("two", () => {}); // test("fake")\nconst s="test( fake";',
                               'helper.ts': 'test("hidden", () => {});'}.items():
                (root / name).write_text(text)
            self.assertEqual(task_l0.static_count(root), 5)


class L0Tests(TaskFixture):
    def receipts(self):
        return [json.loads(p.read_text()) for p in sorted(
            (self.main / '.orcexe/runs').glob('*/attempt-*/l0/result.json'))]

    def checks(self):
        return {c['id']: c for c in self.receipts()[-1]['checks']}

    def seed_files(self, files):
        for name, contents in files.items():
            path = self.main / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '-qm', 'test baseline')
        self.head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        self.saved = self.save()

    def test_all_checks_pass_and_red_first_uses_base_not_candidate(self):
        copies = []
        real = task_l0._tests
        def run(commands, checkout, directory, *args):
            copies.append(checkout)
            self.assertEqual((checkout / 'src/app.py').read_text(), APP)
            expected = TEST.replace('1', '2') if directory.name == 'red' else TEST
            self.assertEqual((checkout / 'tests/test_app.py').read_text(), expected)
            return real(commands, checkout, directory, *args)
        with mock.patch.object(task_l0, '_tests', side_effect=run):
            code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual({c['id']: c['status'] for c in self.receipts()[0]['checks']},
                         {identifier: 'pass' for identifier in task_l0.CHECK_IDS})
        self.assertEqual(len(copies), 2)
        self.assertNotEqual(copies[0], copies[1])
        self.assertFalse(any(p.exists() for p in copies))

    def test_reported_test_count_drop_without_static_test_drop_fails(self):
        load = ('\ndef load_tests(loader, tests, pattern):\n'
                '    return unittest.TestSuite([AppTest("test_app")] * {n})\n')
        self.seed_files({'tests/test_app.py': TEST + load.format(n=2)})
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + load.format(n=1)}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state'], result['attempts']), (1, 'exhausted', 3), result)
        self.assertEqual(self.checks()['test_count_dropped']['status'], 'fail')
        self.assertIn('base=[2]; candidate=[1]', self.checks()['test_count_dropped']['detail'])
        self.assertIn('test_count_dropped', self.briefs[1])


    def test_unknown_count_is_not_seen_and_fails_attempt(self):
        (self.main / '.orcexe/commands.yaml').write_text(
            'version: 1\ncommands:\n  - id: test\n    argv: [python3, -c, "print(123)"]\n')
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['test_count_dropped']['status'], 'not_seen')
        self.assertIsNone(result['land'])

    def test_code_edit_without_test_file_fails(self):
        self.actions.write_text(json.dumps([{'src/app.py': APP + '\n'}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['tests_missing']['status'], 'fail')
        self.assertIn('tests_missing', list(self.log().records(event='l0.failed'))[0]['data']['failed_checks'])
        self.assertIn('tests_missing', self.briefs[1])


    def test_test_change_passing_on_base_fails_red_first(self):
        self.actions.write_text(json.dumps([{'src/app.py': APP + '\n', 'tests/test_app.py': TEST + '\n'}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        checks = self.checks()
        self.assertEqual(checks['tests_missing']['status'], 'pass')
        self.assertEqual(checks['red_first_missing']['status'], 'fail')
        self.assertIn('red_first_missing', self.briefs[1])


    def test_added_focus_marker_uses_slop_and_fails(self):
        self.saved = self.save([plan_task(lane='standard', files={'allow': ['src/**', 'tests/**']})])
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2'),
            'tests/focus.js': "fit('focus', () => { expect(app()).toBe(2); });\n"}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['skip_added']['status'], 'fail')
        self.assertIn('runner_marker', self.checks()['skip_added']['detail'])


    def test_timeout_is_not_red_first_proof(self):
        real = task_l0._tests
        def run(commands, checkout, directory, *args):
            if directory.name == 'red':
                return [dict(id='unittest', count=None, exit_code=None)]
            return real(commands, checkout, directory, *args)
        with mock.patch.object(task_l0, '_tests', side_effect=run):
            code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['red_first_missing']['status'], 'not_seen')




    def test_hollow_tests_fail_attempt_and_supply_assertion_feedback(self):
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('self.assertEqual(app(), 1)', 'return')}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['skip_added']['status'], 'fail')
        self.assertEqual(self.checks()['assertion_missing']['status'], 'fail')
        self.assertIn('assertion_missing', self.briefs[1])
