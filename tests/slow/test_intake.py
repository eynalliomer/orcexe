"""Throughput budgets; enable with --slow or ORCEXE_SLOW_TESTS=1.

Wall/CPU budgets fail on a loaded machine, so the default suite does not run
them. Behaviour coverage remains in tests.unit.test_intake.
"""

import json
import os
import subprocess
import sys
import unittest

from tests.unit._timing import scaled
from tests.unit.test_gitutil import ROOT
from tests.unit.test_intake import IntakeTests


class SlowIntakeTests(IntakeTests):
    def test_this_repository_budget(self):
        # Use the runner's interpreter, including Python 3.9 in compatibility CI.
        # Reaped child CPU excludes time waiting for a busy machine to schedule us.
        if os.name == 'posix':
            import resource
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = subprocess.run(
            [sys.executable, '-c',
             "import sys; sys.path.insert(0, 'plugins/orcexe/src'); "
             "import json; from pathlib import Path; from orcexe import intake; "
             "print(json.dumps(intake.scan(Path.cwd())[1]))"],
            cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        if os.name == 'posix':
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_seconds = (after.ru_utime - before.ru_utime
                           + after.ru_stime - before.ru_stime)
        report = json.loads(result.stdout)
        self.assertGreater(report['size']['tracked_file_count'], 100)
        self.assertIn('CHANGELOG.md', [item['path'] for item in report['docs']])
        if os.name != 'posix':
            self.skipTest('scan/import verified; child CPU budget requires POSIX resource accounting')
        self.assertLess(cpu_seconds, scaled(10))


def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite([SlowIntakeTests('test_this_repository_budget')])
