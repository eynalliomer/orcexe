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
                               # P3-1: a later summary is trusted only as the sum of earlier ones.
                               ('Ran 1 test in 0.01s\nOK\nRan 2 tests in 0.02s\nOK', None),
                               ('Ran 1 test in 0.01s\nOK\nRan 2 tests in 0.02s\nOK\nRan 3 tests in 2 modules in 0.1s', 3)]:
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

    def test_red_first_classification_uses_names_for_tests(self):
        for path in ('tests/test_app.py', 'test_app.py', 'testApp.py', 'app_test.py',
                     'app.test.ts', 'app.spec.jsx', 'test_app.mjs', 'app.test.cjs',
                     'src/app_test.go', 'AppTests.swift', 'AppTest.java',
                     'src/__tests__/app.ts', 'src/__tests__/helpers/util.js'):
            with self.subTest(path=path):
                self.assertEqual(task_l0._red_first_kind(path), 'test')
        for path in ('tests/tools/run_tests.py', 'tests/conftest.py', 'test/helpers.py',
                     'Tests/Fixtures.swift', 'tests/fixtures/data.py'):
            with self.subTest(path=path):
                self.assertEqual(task_l0._red_first_kind(path), 'support')
                self.assertTrue(task_l0.is_test(path))
        for path in ('src/app.py', 'src/helper.ts', 'contest.py', 'docs/testing.md',
                     'tests/data.json', 'testing/helper.py'):
            with self.subTest(path=path):
                self.assertIsNone(task_l0._red_first_kind(path))

    def test_python_added_names_compare_module_bindings(self):
        before = b'def existing():\n    pass\nchanged = 1\n'
        after = (before.replace(b'changed = 1', b'changed = 2') +
                 b'async def added():\n    nested = 1\n'
                 b'class Added:\n    member = 1\n'
                 b'left, (middle, right) = (1, (2, 3))\n'
                 b'[head, *tail] = [1, 2, 3]\n'
                 b'first = second = 1\ntyped: int = 1\n'
                 b'import os.path\nimport sys as system\n'
                 b'from pathlib import Path, PurePath as Pure\n'
                 b'from package import *\nobj.attribute = 1\n'
                 b'obj[[local for local in values]] = 1\n')
        self.assertEqual(task_l0._python_added_names(before, after), {
            'added', 'Added', 'left', 'middle', 'right', 'head', 'tail', 'first', 'second', 'typed',
            'os', 'system', 'Path', 'Pure'})
        for old, new in [(b'def broken(', after), (before, b'def broken('),
                         (b'\xff', after), (before, b'\xff')]:
            with self.subTest(old=old, new=new):
                self.assertEqual(task_l0._python_added_names(old, new), set())

    def test_only_missing_added_support_failures_in_unittest_and_pytest(self):
        imports = "ImportError: cannot import name 'added' from 'tests.helper'"
        attributes = "AttributeError: module 'tests.helper' has no attribute 'added'"
        unittest_error = 'Traceback (most recent call last):\n  File "test.py", line 1\n' + imports + '\n'
        for text in [imports, attributes,
                     unittest_error + 'Ran 1 test in 0.01s\nFAILED (errors=1)\n',
                     'ImportError: Failed to import test module: test_helper\n' + unittest_error +
                     'Ran 1 test in 0.01s\nFAILED (errors=1)\n',
                     unittest_error + 'Traceback (most recent call last):\n' + attributes +
                     '\nRan 2 tests in 0.01s\nFAILED (errors=2)\n',
                     'E   ' + imports + '\n=== 1 error in 0.1s ===\n',
                     'E   ' + attributes + '\n=== 1 failed in 0.1s ===\n',
                     'FAILED tests/test_helper.py::test_added - ' + attributes + '\n1 failed in 0.1s\n']:
            with self.subTest(text=text):
                self.assertTrue(task_l0._only_added_support_failures(text, {'added'}))
        for extra in ['FAILED (failures=1, errors=1)\n',
                      'Ran 2 tests in 2 modules in 0.1s; failures=1 errors=1 skips=0; failed modules=2\n',
                      'Traceback (most recent call last):\nAssertionError: wrong\n',
                      'E   assert 1 == 2\n', 'E   AssertionError\n',
                      'FAILED tests/test_app.py::test_app - assert 1 == 2\n',
                      'Traceback (most recent call last):\nValueError: wrong\n',
                      'Traceback (most recent call last):\nCustomFailure\n',
                      "E   AttributeError: module 'tests.helper' has no attribute 'other'\n",
                      "E   ModuleNotFoundError: No module named 'added'\n",
                      'FAILED (errors=2)\n', 'Traceback (most recent call last):\n']:
            with self.subTest(extra=extra):
                self.assertFalse(task_l0._only_added_support_failures(unittest_error + extra, {'added'}))
        self.assertFalse(task_l0._only_added_support_failures(unittest_error, set()))
        self.assertFalse(task_l0._only_added_support_failures('unrecognized failure', {'added'}))
        # pytest's short summary repeats the error; it is not a second observed failure.
        partial = ('E   ' + attributes + '\nFAILED tests/test_helper.py::test_added - ' + attributes +
                   '\n=== 2 failed in 0.1s ===\n')
        self.assertFalse(task_l0._only_added_support_failures(partial, {'added'}))

    def test_l0_captures_full_output_without_changing_observation_fields(self):
        import tempfile, time
        from types import SimpleNamespace
        text = 'ImportError: missing\n' + 'x' * 5000 + '\nRan 1 test in 0.1s\nFAILED (errors=1)\n'
        command = gate_lite.Command('unittest', ['python3', '-m', 'unittest'])
        outcome = dict(status='exited', pipes_held=False, truncated_lines=False, returncode=1)
        def run(*args, **kwargs):
            for line in text.splitlines():
                kwargs['on_stderr_line'](line)
            return SimpleNamespace(**outcome)
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(task_l0.isolation, 'run', side_effect=run):
            root = Path(raw)
            original = task_l0._tests([command], root, root, 5, time.monotonic() + 30, root)
            captured = task_l0._tests([command], root, root, 5, time.monotonic() + 30, root,
                                      capture_text=True)
            self.assertEqual(captured, [dict(original[0], text=text)])
            self.assertEqual((captured[0]['count'], captured[0]['exit_code']), (1, 1))
            self.assertNotIn('ImportError', (root / 'unittest.log').read_text())
            self.assertEqual(captured[0]['text'], text)
            for incomplete in [dict(status='timeout'), dict(pipes_held=True), dict(truncated_lines=True)]:
                with self.subTest(incomplete=incomplete):
                    outcome.update(incomplete)
                    captured = task_l0._tests([command], root, root, 5, time.monotonic() + 30, root,
                                              capture_text=True)
                    self.assertEqual(captured[0], dict(id='unittest', text='', exit_code=None,
                                                     count=None, skips=None, skip_counts=None))
                    outcome.update(status='exited', pipes_held=False, truncated_lines=False)


    def test_skip_detector_added_markers_and_unchanged_literals(self):
        cases = [
            ('tests/test_app.py', 'import unittest\n' + TEST,
             'import unittest\n' + TEST.replace('    def test_app', '    @unittest.' + 'skip("reason")\n    def test_app'), True),
            ('tests/test_app.py', TEST, TEST + '\n# @unittest.' + 'skip("example")\n', False),
            ('tests/app.js', "test('works', () => {});\n", "test." + "only('works', () => {});\n", True),
            ('tests/app.js', "test\n('works', () => {});\n", "test\n." + "only('works', () => {});\n", True),
            ('tests/app.js', "f" + "it('old', () => {});\n", "// added comment\nf" + "it('old', () => {});\n", False),
            ('tests/app.js', '', 'const text = "test.' + 'only( and x' + 'it(";\n', False),
            ('tests/test_app.py', TEST, 'import pytest\n' + TEST.replace('    def test_app',
              '    @pytest.mark.' + 'xfail(reason="edge")\n    def test_app'), True),
        ]
        for path, before, after, expected in cases:
            with self.subTest(path=path, after=after):
                observed = dict(versions=[(dict(path=path), ('old', 'new'))])
                result = task_l0._skip_added(observed, dict(old=before.encode(), new=after.encode()))
                self.assertEqual(bool(result), expected)

    def test_runtime_final_summary_and_skip_totals(self):
        unseen = dict(count=None, skips=None, skip_counts=None)
        text = 'Ran 1 test in 0.01s\nOK (skipped=1, expected failures=2)\n'
        self.assertEqual(task_l0.runner_summary(text), dict(count=1, skips=3, skip_counts=dict(skipped=1, xfailed=0, expected_failures=2)))
        self.assertEqual(task_l0.runner_summary('1 passed, 2 xfailed in 0.1s'),
                         dict(count=3, skips=2, skip_counts=dict(skipped=0, xfailed=2, expected_failures=0)))
        # P3-1: a later summary that is not the sum of the earlier ones is not trusted.
        self.assertEqual(task_l0.runner_summary('Ran 50 tests in 0.1s\nOK\n' + text), unseen)
        self.assertEqual(task_l0.runner_summary('50 passed in 1.0s\n1 passed, 2 xfailed in 0.1s'), unseen)
        self.assertEqual(task_l0.runner_summary('collected 5 items'), unseen)

    def test_aggregate_summary_of_orcexe_runner_is_trusted_and_measures_skips(self):
        blocks = ('tests.unit.a: tests=3 failures=0 errors=0 skips=1 duration=0.10s\n'
                  '..s\n' + '-' * 70 + '\nRan 3 tests in 0.010s\n\nOK (skipped=1)\n'
                  'tests.unit.b: tests=2 failures=0 errors=0 skips=0 duration=0.20s\n'
                  '..\n' + '-' * 70 + '\nRan 2 tests in 0.020s\n\nOK\n'
                  '10 slowest modules:\n  0.20s tests.unit.b\n  0.10s tests.unit.a\n')
        aggregate = 'Ran 5 tests in 2 modules in 0.31s; failures=0 errors=0 skips=1; failed modules=0\n'
        expected = dict(count=5, skips=1, skip_counts=dict(skipped=1, xfailed=0, expected_failures=0))
        self.assertEqual(task_l0.runner_summary(blocks + aggregate), expected)
        # The aggregate line alone (earlier blocks unparsed) still measures skips.
        self.assertEqual(task_l0.runner_summary(aggregate), expected)
        unseen = dict(count=None, skips=None, skip_counts=None)
        # An atexit handler printing a bigger total after the real aggregate is not trusted.
        forged = blocks + aggregate + 'Ran 999 tests in 0.001s\n\nOK\n'
        self.assertEqual(task_l0.runner_summary(forged), unseen)
        # T52c: the runner prints only some module blocks, so a sole aggregate
        # line is authoritative even when the visible blocks do not add up to it.
        self.assertEqual(task_l0.runner_summary(blocks + aggregate.replace('Ran 5', 'Ran 7')),
                         dict(expected, count=7))
        self.assertEqual(task_l0.runner_summary(blocks + aggregate.replace('skips=1', 'skips=0')),
                         dict(count=5, skips=0, skip_counts=dict(skipped=0, xfailed=0, expected_failures=0)))
        # A plain summary of any format after the aggregate is not trusted.
        self.assertEqual(task_l0.runner_summary(blocks + aggregate + '== 999 passed in 0.01s ==\n'), unseen)
        # A second aggregate after the real one, or one printed early by a test, is not trusted.
        self.assertEqual(task_l0.runner_summary(blocks + aggregate + aggregate.replace('Ran 5', 'Ran 999')), unseen)
        early = aggregate.replace('Ran 5', 'Ran 999')
        self.assertEqual(task_l0.runner_summary(early + blocks + aggregate), unseen)
        self.assertEqual(task_l0.runner_summary(blocks.replace('..s\n', '..s\n' + early, 1) + aggregate), unseen)
        # pytest single-line summaries keep working.
        self.assertEqual(task_l0.runner_summary('== 4 passed, 1 skipped in 0.20s =='),
                         dict(count=5, skips=1, skip_counts=dict(skipped=1, xfailed=0, expected_failures=0)))

    def test_real_orcexe_runner_logs_trust_the_aggregate_line(self):
        # T52c: tails of real run A logs (gate, L0 base, L0 red); the visible module
        # blocks sum to far less than the aggregate, which must still be trusted.
        directory = Path(__file__).resolve().parents[1] / 'fixtures' / 'runner-summary'
        for name, count, skips in [('gate.log', 991, 19), ('base.log', 988, 19), ('red.log', 991, 19)]:
            with self.subTest(name=name):
                text = (directory / name).read_text(encoding='utf-8')
                self.assertEqual(task_l0.runner_summary(text), dict(
                    count=count, skips=skips, skip_counts=dict(skipped=skips, xfailed=0, expected_failures=0)))
        self.assertIn('failures=3', (directory / 'red.log').read_text(encoding='utf-8'))
        text = (directory / 'gate.log').read_text(encoding='utf-8')
        unseen = dict(count=None, skips=None, skip_counts=None)
        self.assertEqual(task_l0.runner_summary(text + 'Ran 5000 tests in 0.001s\n\nOK\n'), unseen)
        self.assertEqual(task_l0.runner_summary(text + text.splitlines()[-1].replace('991', '5000') + '\n'), unseen)

    def test_plain_unittest_and_pytest_without_aggregate_keep_t52a_rule(self):
        unseen = dict(count=None, skips=None, skip_counts=None)
        zero = dict(skipped=0, xfailed=0, expected_failures=0)
        self.assertEqual(task_l0.runner_summary('Ran 4 tests in 0.1s\n\nOK\n'), dict(count=4, skips=0, skip_counts=zero))
        two = 'Ran 1 test in 0.01s\nOK\nRan 2 tests in 0.02s\nOK (skipped=1)\n'
        self.assertEqual(task_l0.runner_summary(two), unseen)
        self.assertEqual(task_l0.runner_summary(two + 'Ran 3 tests in 0.03s\nOK (skipped=1)\n'),
                         dict(count=3, skips=1, skip_counts=dict(zero, skipped=1)))
        self.assertEqual(task_l0.runner_summary('==== 3 passed, 1 failed, 2 skipped in 1.5s ===='),
                         dict(count=6, skips=2, skip_counts=dict(zero, skipped=2)))

    def test_l0_runs_declared_interpreter_and_missing_one_is_not_seen(self):
        import sys, tempfile, time
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            checkout, logs = root / 'checkout', root / 'logs'
            checkout.mkdir()
            command = gate_lite.Command('unittest', ['python3', '-c', 'print("Ran 2 tests in 0.1s"); print("OK")'])
            missing = task_l0._tests([command], checkout, logs, 5, time.monotonic() + 30, root,
                                     dict(declared='3.43', path=None))
            self.assertEqual(missing, [dict(id='unittest', count=None, skips=None, skip_counts=None, exit_code=None)])
            self.assertIn('interpreter_unavailable: python 3.43', (logs / 'unittest.log').read_text())
            resolved = task_l0._tests([command], checkout, logs, 5, time.monotonic() + 30, root,
                                      dict(declared='3.9', path=sys.executable))
            self.assertEqual((resolved[0]['count'], resolved[0]['exit_code']), (2, 0))

    def test_each_runtime_skip_and_hollow_form(self):
        cases = ['raise unittest.' + 'SkipTest("reason")', 'pytest.' + 'skip("reason")',
                 'pytest.' + 'xfail("reason")', 'pytest.' + 'importorskip("missing")', 'return', 'pass', '...']
        for body in cases:
            after = 'def test_example():\n    ' + body + '\n'
            observed = dict(versions=[(dict(path='tests/test_example.py'), ('old', 'new'))])
            with self.subTest(body=body):
                self.assertTrue(task_l0._skip_added(observed, dict(old=b'', new=after.encode())))
        alias = 'from unittest import ' + 'expectedFailure as marker\n@marker\ndef test_example():\n    assert True\n'
        self.assertTrue(task_l0._skip_added(observed, dict(old=b'', new=alias.encode())))
        for decorator in ('unittest.' + 'expectedFailure', 'pytest.mark.' + 'xfail'):
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

    def test_shared_check_vocabulary_and_hollow_controls(self):
        python_checks = [
            'assert value', 'self.assertEqual(value, 2)', 'assertEquals(value, 2)',
            'context.expectEqual(value, 2)', 'context.shouldMatch(value)', 'verifyResult(value)',
            'fail("wrong")', 'self.fail("wrong")', 't.fail("wrong")',
            'with pytest.raises(ValueError):\n        product()',
            'with self.assertRaises(ValueError):\n        product()',
            'if product() != 2:\n        raise AssertionError("wrong")',
            'raise ValueError("wrong")',
            'return self.assertEqual(product(), 2)',
        ]
        js_checks = [
            'assert(value);', 'assert.equal(value, 2);', 'context.assertEquals(value, 2);',
            'expect(value).toBe(2);', 'context.shouldMatch(value);', 'verifyResult(value);',
            'fail("wrong");', 't.fail("wrong");', 'self.fail("wrong");',
            'assertThrows(() => product());', 'assertRejects(() => product());',
            'if (product() !== 2) { throw new Error("wrong"); }', 'throw new Error("wrong");',
            'return expect(product()).toBe(2);',
        ]
        cases = [('tests/test_checks.py', 'def test_check():\n    ' + body + '\n', True)
                 for body in python_checks]
        cases += [('checks.test.ts', 'Deno.test("check", () => { ' + body + ' });\n', True)
                  for body in js_checks]
        cases += [('Tests/Checks.swift', 'func testCheck() throws { ' + body + ' }\n', True)
                  for body in ('XCTAssertEqual(value, 2)', 'XCTFail("wrong")', '#expect(value == 2)',
                               '#require(value == 2)', 'if value != 2 { throw Wrong.value }')]
        cases += [('tests/test_checks.py', 'def test_check():\n    product()\n', False),
                  ('checks.test.ts', 'Deno.test("check", () => { product(); });\n', False),
                  ('Tests/Checks.swift', 'func testCheck() { product() }\n', False)]
        for path, source, checks in cases:
            with self.subTest(path=path, source=source):
                observed = dict(versions=[(dict(path=path), ('old', 'new'))])
                blobs = dict(old=b'', new=source.encode())
                self.assertEqual(bool(task_l0._assertion_missing(observed, blobs)), not checks)
                self.assertEqual(bool(task_l0._skip_added(observed, blobs)), not checks)

    def test_shared_checks_do_not_borrow_literals_or_uncalled_helpers(self):
        for path, source in [
                ('tests/test_checks.py', 'def test_check():\n    text = "raise AssertionError()"\n    product()\n'),
                ('tests/test_checks.py', 'def test_check():\n    def helper():\n        raise AssertionError()\n    product()\n'),
                ('checks.test.ts', 'test("check", () => { const s = "throw Error()"; product(); });'),
                ('checks.test.ts', 'test("check", () => { function helper() { throw Error(); } product(); });'),
                ('Tests/Checks.swift', 'func testCheck() { func helper() { XCTFail("wrong") }; product() }')]:
            with self.subTest(source=source):
                observed = dict(versions=[(dict(path=path), ('old', 'new'))])
                blobs = dict(old=b'', new=source.encode())
                self.assertTrue(task_l0._assertion_missing(observed, blobs))
                self.assertTrue(task_l0._skip_added(observed, blobs))


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
        # Ordinary red runs retain the runner's positional call contract.
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

    def test_raise_style_unittest_passes_all_l0_checks(self):
        updated = TEST.replace('self.assertEqual(app(), 1)',
                               'if app() != 2:\n            raise AssertionError("wrong result")')
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
                                            'tests/test_app.py': updated}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual({c['id']: c['status'] for c in self.receipts()[0]['checks']},
                         {identifier: 'pass' for identifier in task_l0.CHECK_IDS})

    def support_test(self, expected):
        return ('import runpy\nimport unittest\nfrom pathlib import Path\n'
                'class SupportTest(unittest.TestCase):\n'
                '    def test_support(self):\n'
                '        support = runpy.run_path(str(Path(__file__).parent / "tools/run_tests.py"))\n'
                '        self.assertEqual(support["value"](), ' + str(expected) + ')\n')

    def test_modified_support_stays_at_base_and_proves_red_first(self):
        self.seed_files({'tests/tools/run_tests.py': 'def value():\n    return 1\n'})
        self.actions.write_text(json.dumps([{
            'src/app.py': APP + '\n',
            'tests/tools/run_tests.py': 'def value():\n    return 2\n',
            'tests/test_support.py': self.support_test(2)}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual({c['id']: c['status'] for c in self.receipts()[0]['checks']},
                         {identifier: 'pass' for identifier in task_l0.CHECK_IDS})

    def test_noop_modified_support_does_not_prove_red_first(self):
        support = 'def value():\n    return 1\n'
        self.seed_files({'tests/tools/run_tests.py': support})
        self.actions.write_text(json.dumps([{
            'src/app.py': APP + '\n',
            'tests/tools/run_tests.py': support + '\n# no behaviour change\n',
            'tests/test_support.py': self.support_test(1)}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['red_first_missing']['status'], 'fail')
        self.assertEqual(self.checks()['red_first_missing']['detail'], 'no_observed_failure_on_base')

    def test_added_support_is_overlaid_without_fake_import_failure(self):
        self.actions.write_text(json.dumps([{
            'src/app.py': APP + '\n',
            'tests/helper.py': 'def value():\n    return 1\n',
            'tests/test_support.py': (
                'import unittest\nfrom tests.helper import value\n'
                'class SupportTest(unittest.TestCase):\n'
                '    def test_support(self):\n        self.assertEqual(value(), 1)\n')}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['red_first_missing']['status'], 'fail')
        self.assertEqual(self.checks()['red_first_missing']['detail'], 'no_observed_failure_on_base')

    def test_modified_support_new_import_does_not_prove_red_first(self):
        support = 'def value():\n    return 1\n'
        self.seed_files({'tests/helper.py': support})
        self.actions.write_text(json.dumps([{
            'src/app.py': APP + '\n',
            'tests/helper.py': support + '\ndef added():\n    return 2\n',
            'tests/test_support.py': (
                'import unittest\nfrom tests.helper import added\n'
                'class SupportTest(unittest.TestCase):\n'
                '    def test_support(self):\n        self.assertEqual(added(), 2)\n')}]))
        write = task_l0.safe_write_text
        red_logs = []
        def forge_log(path, text, *args, **kwargs):
            if path.parent.name == 'red':
                red_logs.append(path)
                text = 'AssertionError: forged log\n'
            return write(path, text, *args, **kwargs)
        with mock.patch.object(task_l0, 'safe_write_text', side_effect=forge_log):
            code, result, _ = self.invoke()
        self.assertTrue(red_logs)
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['red_first_missing'], dict(
            id='red_first_missing', status='fail', detail='only_added_support_names_missing_on_base'))

    def test_tests_only_modified_support_new_import_is_exempt(self):
        support = 'def value():\n    return 1\n'
        self.seed_files({'tests/helper.py': support})
        self.actions.write_text(json.dumps([{
            'tests/helper.py': support + '\ndef added():\n    return 2\n',
            'tests/test_support.py': (
                'import unittest\nfrom tests.helper import added\n'
                'class SupportTest(unittest.TestCase):\n'
                '    def test_support(self):\n        self.assertEqual(added(), 2)\n')}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual(self.checks()['red_first_missing'], dict(
            id='red_first_missing', status='pass', detail='tests_only'))
        self.assertTrue(self.receipts()[-1]['passed'])

    def test_modified_support_new_attribute_does_not_prove_red_first(self):
        support = 'def value():\n    return 1\n'
        self.seed_files({'tests/helper.py': support})
        self.actions.write_text(json.dumps([{
            'src/app.py': APP + '\n',
            'tests/helper.py': support + '\ndef added():\n    return 2\n',
            'tests/test_support.py': (
                'import unittest\nfrom tests import helper\n'
                'class SupportTest(unittest.TestCase):\n'
                '    def test_support(self):\n        self.assertEqual(helper.added(), 2)\n')}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['red_first_missing'], dict(
            id='red_first_missing', status='fail', detail='only_added_support_names_missing_on_base'))

    def test_modified_support_assertion_counts_despite_new_helper_import_error(self):
        self.seed_files({'tests/tools/run_tests.py': 'def value():\n    return 1\n'})
        self.actions.write_text(json.dumps([{
            'src/app.py': APP + '\n',
            'tests/tools/run_tests.py': 'def value():\n    return 2\n\ndef added():\n    return 2\n',
            'tests/test_support.py': self.support_test(2),
            'tests/test_added.py': (
                'import unittest\nfrom tests.tools.run_tests import added\n'
                'class AddedTest(unittest.TestCase):\n'
                '    def test_added(self):\n        self.assertEqual(added(), 2)\n')}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual(self.checks()['red_first_missing'], dict(
            id='red_first_missing', status='pass', detail='tests_fail_on_base'))

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

    def test_standard_gate_declared_missing_interpreter_is_not_seen(self):
        self.seed_files({'.python-version': '3.43\n'})
        with mock.patch.object(gate_lite, '_PYTHON_LOCATIONS', ()):
            code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertIsNone(result['land'])
        gates = [json.loads(p.read_text()) for p in sorted(
            (self.main / '.orcexe/runs').glob('*/attempt-*/gate.json'))]
        self.assertTrue(gates)
        last = gates[-1]['checks'][-1]
        self.assertEqual(last['id'], 'project_checks')
        self.assertIn('interpreter_unavailable: python 3.43', last['detail'])

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
            'tests/focus.js': "f" + "it('focus', () => { expect(app()).toBe(2); });\n"}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['skip_added']['status'], 'fail')
        self.assertIn('runner_marker', self.checks()['skip_added']['detail'])


    def test_timeout_is_not_red_first_proof(self):
        real = task_l0._tests
        def run(commands, checkout, directory, *args, **kwargs):
            if directory.name == 'red':
                return [dict(id='unittest', count=None, exit_code=None)]
            return real(commands, checkout, directory, *args, **kwargs)
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
