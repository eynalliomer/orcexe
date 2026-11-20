"""Extended end-to-end matrices; enable with --slow or ORCEXE_SLOW_TESTS=1.

Fast examples and pure validators remain in tests.unit.test_task_l0.
"""

import os
import unittest
from tests.unit.test_task_l0 import *

class SlowL0Tests(L0Tests):
    test_all_checks_pass_and_red_first_uses_base_not_candidate = None
    test_reported_test_count_drop_without_static_test_drop_fails = None
    test_unknown_count_is_not_seen_and_fails_attempt = None
    test_code_edit_without_test_file_fails = None
    test_test_change_passing_on_base_fails_red_first = None
    test_added_focus_marker_uses_slop_and_fails = None
    test_timeout_is_not_red_first_proof = None
    test_hollow_tests_fail_attempt_and_supply_assertion_feedback = None

    def test_failing_base_still_supplies_a_known_count(self):
        self.seed_files({'tests/test_app.py': TEST.replace('1', '2')})
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + '\n# regression expectation\n'}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual(self.checks()['test_count_dropped']['status'], 'pass')
        self.assertEqual(self.checks()['red_first_missing']['status'], 'pass')


    def test_doc_only_does_not_require_test_file(self):
        self.saved = self.save([plan_task(lane='standard', files={'allow': ['README.md']})])
        self.actions.write_text(json.dumps([{'README.md': 'Changed docs\n'}]))
        self.invoke()
        self.assertEqual(self.checks()['tests_missing']['status'], 'pass')


    def test_new_test_is_overlaid_on_base(self):
        new = TEST.replace('test_app', 'test_new').replace('1', '2')
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2'), 'tests/test_new.py': new}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual(self.checks()['red_first_missing']['status'], 'pass')


    def test_old_markers_and_literal_marker_text_are_not_added_skips(self):
        js = "fit('old', () => { expect(app()).toBe(1); });\n"
        self.seed_files({'tests/focus.js': js})
        self.saved = self.save([plan_task(lane='standard', files={'allow': ['src/**', 'tests/**']})])
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + '\n# unittest.skip is documentation\n',
            'tests/focus.js': js + 'const text = "fit(";\n'}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual(self.checks()['skip_added']['status'], 'pass')


    def test_static_count_drop_survives_spoofed_runtime_summary(self):
        self.seed_files({'tests/test_more.py': TEST})
        spoof = '\nimport sys\nprint("Ran 50 tests in 0.001s", file=sys.stderr)\n'
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + spoof, 'tests/test_more.py': None,
            'tests/hidden.py': TEST}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['test_count_dropped']['status'], 'fail')
        self.assertEqual(self.checks()['test_count_dropped']['detail'], 'static base=2; candidate=1')


    def test_runtime_skips_increase_without_literal_skip_marker(self):
        # An indirect runner-level skip is invisible to source marker matching.
        extra = ('\noriginal = AppTest.test_app\n'
                 'AppTest.test_app = getattr(unittest, "sk" + "ip")("runtime")(original)\n')
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + extra}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertEqual(self.checks()['skip_added']['status'], 'fail')
        self.assertEqual(self.checks()['skip_added']['detail'], 'runtime_skips_increased')


    def test_new_symbol_import_is_known_weak_red_evidence(self):
        self.actions.write_text(json.dumps([{'src/app.py': APP + '\ndef new_symbol():\n    return 2\n',
            'tests/test_app.py': TEST + '\nfrom src.app import new_symbol\n'}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertEqual(self.checks()['red_first_missing']['status'], 'pass')



def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in (SlowL0Tests,))
