"""Throughput budgets; enable with --slow or ORCEXE_SLOW_TESTS=1.

Wall/CPU budgets fail on a loaded machine, so the default suite does not run
them. Behaviour coverage remains in tests.unit.test_ledger.
"""

import hashlib
import os
import time
import unittest

from orcexe import ledger
from tests.unit._timing import scaled
from tests.unit.test_ledger import EVENTS, LedgerTests


class SlowLedgerTests(LedgerTests):
    def test_verify_100000_records_within_scaled_four_seconds(self):
        # Bulk fixture creation avoids timing 100k fsyncs; every link is real.
        self.path.parent.mkdir()
        record = {"schema": "orcexe.ledger.v1", "seq": 1, "ts": "2026-11-16T21:30:00.123Z",
                  "event": "task.state", "task": "T1", "data": dict(EVENTS["task.state"]), "prev": "0" * 64}
        with self.path.open("wb") as handle:
            for number in range(1, 100001):
                record["seq"] = number
                raw = ledger._serialize(record)
                handle.write(raw + b"\n")
                record["prev"] = hashlib.sha256(raw).hexdigest()
        start = time.monotonic()
        self.assertEqual(self.ledger.verify(), {"records": 100000, "last_seq": 100000, "ok": True})
        self.assertLess(time.monotonic() - start, scaled(4))


def load_tests(loader, standard_tests, pattern):
    if os.environ.get("ORCEXE_SLOW_TESTS") != "1":
        return unittest.TestSuite()
    return unittest.TestSuite([SlowLedgerTests('test_verify_100000_records_within_scaled_four_seconds')])
