"""Scanned user Python is parsed without leaking SyntaxWarning/DeprecationWarning."""

import ast
import contextlib
import io
import unittest
import warnings

from orcexe import gate_lite, slop, task_l0

PATH = 'tests/test_sample.py'
# "\d" is an invalid escape: SyntaxWarning on 3.12+, DeprecationWarning before.
HOLLOW = '\n\ndef test_hollow():\n    pass\n'  # gives slop a fake_test finding to compare
NOISY = 'import re\n\n\ndef test_digits():\n    assert re.match("\\d+", "42")\n' + HOLLOW
QUIET = 'import re\n\n\ndef test_digits():\n    assert re.match("\\\\d+", "42")\n' + HOLLOW
WARNINGS = (SyntaxWarning, DeprecationWarning)


def noisy_warnings(action):
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always')
        action()
    return [item for item in caught if issubclass(item.category, WARNINGS)]


def stderr_of(action):
    buffer = io.StringIO()
    with contextlib.redirect_stderr(buffer), warnings.catch_warnings():
        warnings.simplefilter('always')
        action()
    return buffer.getvalue()


def slop_findings(text):
    found = []
    summary = slop._python(PATH, text, lambda *item: found.append(item))
    return found, summary


class AstWarningsTest(unittest.TestCase):
    def test_control_plain_parse_warns(self):
        self.assertTrue(noisy_warnings(lambda: ast.parse(NOISY)))
        self.assertIn('invalid escape sequence', stderr_of(lambda: ast.parse(NOISY)))

    def test_helper_records_no_warning(self):
        self.assertEqual(noisy_warnings(lambda: gate_lite.parse_python(NOISY)), [])

    def test_stderr_is_empty_for_every_call_site(self):
        for name, action in (
            ('helper', lambda: gate_lite.parse_python(NOISY)),
            ('_python_code', lambda: gate_lite._python_code(NOISY)),
            ('_python_tests', lambda: task_l0._python_tests(PATH, NOISY)),
            ('slop unit', lambda: slop._PythonAssertions({PATH: NOISY}).unit(PATH)),
            ('slop scan', lambda: slop_findings(NOISY)),
        ):
            with self.subTest(name):
                self.assertEqual(stderr_of(action), '')
                self.assertEqual(noisy_warnings(action), [])

    def test_warnings_as_errors_do_not_become_syntax_errors(self):
        with warnings.catch_warnings():
            warnings.simplefilter('error')
            with self.assertRaises(SyntaxError):
                ast.parse(NOISY)
            self.assertEqual(ast.dump(gate_lite.parse_python(NOISY)),
                             ast.dump(gate_lite.parse_python(QUIET)))

    def test_tree_is_identical_to_plain_parse(self):
        for text in (NOISY, QUIET):
            with self.subTest(text=text):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore')
                    expected = ast.dump(ast.parse(text))
                self.assertEqual(ast.dump(gate_lite.parse_python(text)), expected)

    def test_results_match_warning_free_source(self):
        self.assertEqual(gate_lite._python_code(NOISY), gate_lite._python_code(QUIET))
        noisy_units = task_l0._python_tests(PATH, NOISY)
        quiet_units = task_l0._python_tests(PATH, QUIET)
        self.assertEqual([name for name, _ in noisy_units], ['test_digits', 'test_hollow'])
        self.assertEqual([(name, ast.dump(node)) for name, node in noisy_units],
                         [(name, ast.dump(node)) for name, node in quiet_units])
        self.assertEqual(slop_findings(NOISY), slop_findings(QUIET))
        self.assertEqual([item[0] for item in slop_findings(NOISY)[0]], ['fake_test'])
        noisy_unit = slop._PythonAssertions({PATH: NOISY}).unit(PATH)
        quiet_unit = slop._PythonAssertions({PATH: QUIET}).unit(PATH)
        self.assertEqual(ast.dump(noisy_unit[0]), ast.dump(quiet_unit[0]))

    def test_real_syntax_error_is_still_raised_and_handled(self):
        broken = 'def broken(:\n    pass\n'
        for call in (gate_lite.parse_python, gate_lite._python_code,
                     lambda text: task_l0._python_tests(PATH, text)):
            with self.assertRaises(SyntaxError):
                call(broken)
        self.assertIsNone(slop._PythonAssertions({PATH: broken}).unit(PATH))

    def test_suppression_is_scoped_to_the_parse_call(self):
        with warnings.catch_warnings():
            warnings.simplefilter('always')
            before = list(warnings.filters)
            gate_lite.parse_python(NOISY)
            self.assertEqual(warnings.filters, before)
            with self.assertRaises(SyntaxError):
                gate_lite.parse_python('def broken(:\n')
            self.assertEqual(warnings.filters, before)
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter('always')
                warnings.warn('later', DeprecationWarning)
            self.assertEqual([str(item.message) for item in caught], ['later'])


if __name__ == '__main__':
    unittest.main()
