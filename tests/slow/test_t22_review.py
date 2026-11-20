"""Extended end-to-end matrices; enable with --slow or ORCEXE_SLOW_TESTS=1.

Fast examples and pure validators remain in tests.unit.test_t22_review.
"""

import os
import unittest
from tests.unit.test_t22_review import *

class SlowClosingReviewTests(ClosingReviewTests):
    test_b1_codex_plant_never_runs_and_reports_main_changed = None
    test_b1_existing_config_byte_changes_are_detected = None
    test_b1_gate_receives_frozen_commands_even_if_file_appears_later = None
    test_b2_claude_inject_quoted_auth_is_accepted = None
    test_b2_agent_inject_quoted_markers_is_accepted = None
    test_b2_only_structured_errors_and_non_json_diagnostics_classify = None
    test_b3_aged_cache_routes_claude_and_doctor_shows_age = None
    test_b3_reset_window_and_unknown_quota_do_not_exhaust_route = None
    test_b4_agent_symlink_probe_and_parent_attack_leave_victim_unchanged = None
    test_p2_gate_uses_state_dir_and_briefs_are_private = None
    test_b4_all_run_writers_refuse_file_and_directory_symlinks = None
    test_b5_zero_unittest_and_pytest_evidence_is_not_seen = None
    test_p2_manual_run_pipes_held_is_exit_two_and_visible = None

    def test_b5_unittest_layouts_pass_fail_and_zero_under_available_pythons(self):
        interpreters = {str(Path(sys.executable).resolve()), '/usr/bin/python3'}
        for index, interpreter in enumerate(sorted(interpreters)):
            if not Path(interpreter).exists():
                continue
            bindir = self.root / ('python-' + str(index))
            bindir.mkdir()
            (bindir / 'python3').symlink_to(interpreter)
            self.env['PATH'] = str(bindir) + os.pathsep + self.executor_env['PATH']
            expected_python = subprocess.run(
                [str(bindir / 'python3'), '-c', 'import sys; print(sys.executable)'],
                env=self.env, capture_output=True, text=True, check=True, timeout=scaled(5)).stdout.strip()
            for packaged in (False, True):
                init = self.repo / 'tests/__init__.py'
                if packaged:
                    init.touch()
                else:
                    init.unlink(missing_ok=True)
                for status, assertion, verdict in (
                        ('pass', 'self.assertIsNone(app())', 'accepted'),
                        ('fail', 'self.assertIsNotNone(app())', 'needs_fix'),
                        ('not_seen', None, 'unverified')):
                    with self.subTest(interpreter=interpreter, packaged=packaged, status=status):
                        # Match mkrepo: unpackaged tests import app via src on sys.path.
                        source = 'import sys, unittest\nsys.path.insert(0, "src")\nfrom app import app\n'
                        if assertion:
                            source += ('class AppTest(unittest.TestCase):\n'
                                       '    def test_app(self):\n        ' + assertion + '\n')
                        (self.repo / 'tests/test_app.py').write_text(source)
                        self.git('add', 'tests')
                        self.git('commit', '-qm', 'unittest discovery case')
                        base = self.git('rev-parse', 'HEAD')
                        command, = gl.detect_commands(self.repo, base)
                        self.assertEqual(command.argv,
                            ['python3', '-m', 'unittest', 'discover', '-s', 'tests']
                            + (['-t', '.'] if packaged else []))
                        result = self.invoke()
                        self.assertEqual(result['verdict'], self.accepted_verdict if verdict == 'accepted' else verdict, result)
                        directory = self.repo / '.orcexe/runs' / result['run_id']
                        gate = json.loads((directory / 'gate.json').read_text())
                        project = next(c for c in gate['checks'] if c['id'] == 'project_checks')
                        self.assertEqual(project['status'], status, gate)
                        log = (directory / 'gate/unittest.log').read_text()
                        self.assertIn('Ran 0 tests' if status == 'not_seen' else 'Ran 1 test', log)
                        identity = (directory / 'gate/interpreters.log').read_text()
                        self.assertIn('gate sys.executable: ' + sys.executable, identity)
                        self.assertIn('PATH python3 sys.executable: ' + expected_python, identity)



def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in (SlowClosingReviewTests,))
