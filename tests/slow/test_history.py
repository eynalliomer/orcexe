"""Throughput budgets; enable with --slow or ORCEXE_SLOW_TESTS=1.

Wall/CPU budgets fail on a loaded machine, and these scan this repository's own
git history, which a gate checkout (a plain file tree without .git) does not
have, so the default suite does not run them. Behaviour coverage remains in
tests.unit.test_history.
"""

import json
import os
import subprocess
import sys
import time
import unittest

from orcexe import history
from tests.unit._timing import scaled
from tests.unit.test_gitutil import ROOT
from tests.unit.test_history import HistoryTests


class SlowHistoryTests(HistoryTests):
    def test_repository_child_cpu_and_python39_import(self):
        # Use the runner's interpreter, including Python 3.9 in compatibility CI.
        # Reaped child CPU excludes time waiting for a busy machine to schedule us.
        if os.name == 'posix':
            import resource
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = subprocess.run([sys.executable, '-c', 'from orcexe.history import scan; from pathlib import Path; import json; print(json.dumps(scan(Path.cwd())[1]))'],
                                cwd=ROOT, env=dict(os.environ, PYTHONPATH=str(ROOT / 'plugins/orcexe/src')),
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
        self.assertEqual(json.loads(result.stdout)['u2']['schema'], 'orcexe.intake.u2.v1')
        if os.name == 'posix':
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            self.assertLess(after.ru_utime + after.ru_stime - before.ru_utime - before.ru_stime,
                            scaled(10))

    def test_wall_budget(self):
        start = time.monotonic()
        history.scan(ROOT)
        self.assertLess(time.monotonic() - start, scaled(10))


def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite([SlowHistoryTests(name) for name in
                               ('test_repository_child_cpu_and_python39_import',
                                'test_wall_budget')])
