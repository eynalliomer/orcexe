"""Throughput budgets; enable with --slow or ORCEXE_SLOW_TESTS=1.

Wall/CPU budgets fail on a loaded machine, so the default suite does not run
them. Behaviour coverage remains in tests.unit.test_slop.
"""

import json
import os
import subprocess
import sys
import time
import unittest

from orcexe import slop
from tests.unit._timing import scaled
from tests.unit.test_gitutil import ROOT
from tests.unit.test_slop import SlopTests


class SlowSlopTests(SlopTests):
    def test_python39_import_and_repo_scan_budget(self):
        # Use the runner's interpreter, including Python 3.9 in compatibility CI.
        # Reaped child CPU excludes time waiting for a busy machine to schedule us.
        if os.name == 'posix':
            import resource
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = subprocess.run(
            [sys.executable, '-c',
             "import sys; sys.path.insert(0, 'plugins/orcexe/src'); "
             "import json; from pathlib import Path; from orcexe import slop; "
             "print(json.dumps(slop.scan(Path.cwd())[1]))"],
            cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        if os.name == 'posix':
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_seconds = (after.ru_utime - before.ru_utime
                           + after.ru_stime - before.ru_stime)
        report = json.loads(result.stdout)
        self.assertEqual(set(report['counts']), set(slop.CLASSES))
        if os.name != 'posix':
            self.skipTest('scan/import verified; child CPU budget requires POSIX resource accounting')
        self.assertLess(cpu_seconds, scaled(10))

    def test_repository_scan_wall_clock_budget(self):
        started = time.monotonic()
        report = slop.scan(ROOT)[1]
        self.assertLess(time.monotonic() - started, scaled(10))
        self.assertLess(report['meta']['duration_ms'], scaled(10000))


def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite([SlowSlopTests(name) for name in
                               ('test_python39_import_and_repo_scan_budget',
                                'test_repository_scan_wall_clock_budget')])
