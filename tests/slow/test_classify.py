"""Throughput budgets; enable with --slow or ORCEXE_SLOW_TESTS=1.

Wall/CPU budgets fail on a loaded machine, so the default suite does not run
them. Behaviour coverage remains in tests.unit.test_classify.
"""

import os
import time
import unittest

from orcexe.classify import classify
from tests.unit._timing import scaled
from tests.unit.test_classify import ClassifyTests


class SlowClassifyTests(ClassifyTests):
    def test_100000_paths_performance(self):
        files = ["src/part{}/module{}.py".format(i // 100, i) for i in range(100000)]
        task = "Fix src/part999/module99999.py " + "small " * 328 + "x" * 3
        task = task[:2000].ljust(2000)
        self.assertEqual(len(task), 2000)
        started = time.process_time()
        result = classify(task, files)
        elapsed = time.process_time() - started
        self.assertEqual(result.lane, "standard")
        self.assertIn("task_too_long_for_fast", {s.id for s in result.signals})
        self.assertEqual(classify("Add a docstring to src/part999/module99999.py", files).lane, "fast")
        self.assertLess(elapsed, scaled(.5))
        print("classifier 100000-path CPU time: {:.4f}s".format(elapsed))


def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite([SlowClassifyTests('test_100000_paths_performance')])
