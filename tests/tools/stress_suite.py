#!/usr/bin/env python3
"""Repeat the test suite under CPU load (stdlib only, Python 3.9).

Run from the repository root:
    python3 tests/tools/stress_suite.py --runs 5 --scale 3
"""

import argparse
import math
import os
from pathlib import Path
import subprocess
import sys
import time


SPIN = """
import os
try:
    os.nice(10)
except (AttributeError, OSError):
    pass
while True:
    pass
"""


def load_average():
    try:
        return os.getloadavg()
    except (AttributeError, OSError):
        return "unavailable"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    parser.add_argument("--scale", type=float, default=3)
    parser.add_argument("--runner", choices=("parallel", "discover"), default="parallel")
    args = parser.parse_args()
    if args.runs <= 0:
        parser.error("--runs must be > 0")
    if not math.isfinite(args.scale) or args.scale <= 0:
        parser.error("--scale must be finite and > 0")

    root = Path(__file__).resolve().parents[2]
    environment = dict(os.environ, ORCEXE_TEST_TIME_SCALE=str(args.scale))
    workers = []
    suite = None
    passed = 0
    interrupted = False
    print("load average at start: {}".format(load_average()), flush=True)
    try:
        for _ in range(2 * (os.cpu_count() or 1)):
            workers.append(subprocess.Popen(
                [sys.executable, "-c", SPIN], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        print("busy workers: {}; time scale: {}".format(len(workers), args.scale),
              flush=True)
        for index in range(1, args.runs + 1):
            before = load_average()
            started = time.monotonic()
            suite = subprocess.Popen(
                ([sys.executable, str(root / "tests/tools/run_tests.py")]
                 if args.runner == "parallel" else
                 [sys.executable, "-m", "unittest", "discover"]),
                cwd=str(root), env=environment, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            output, _ = suite.communicate()
            duration = time.monotonic() - started
            success = suite.returncode == 0
            passed += success
            print("run {}/{}: {} ({:.2f}s)".format(
                index, args.runs, "PASS" if success else "FAIL", duration), flush=True)
            print("  load before: {}; after: {}".format(before, load_average()), flush=True)
            if not success:
                print(output, flush=True)
    except KeyboardInterrupt:
        interrupted = True
        print("interrupted", flush=True)
    finally:
        print("load average at end: {}".format(load_average()), flush=True)
        # Signal all workers first, then reap them; also covers partial startup.
        for worker in workers:
            if worker.poll() is None:
                worker.terminate()
        if suite is not None and suite.poll() is None:
            suite.terminate()
            try:
                suite.wait(timeout=5)
            except subprocess.TimeoutExpired:
                suite.kill()
                suite.wait()
        if suite is not None and suite.stdout is not None:
            suite.stdout.close()
        for worker in workers:
            try:
                worker.wait(timeout=5)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()
    print("passed {}/{}".format(passed, args.runs), flush=True)
    return 130 if interrupted else int(passed != args.runs)


if __name__ == "__main__":
    sys.exit(main())
