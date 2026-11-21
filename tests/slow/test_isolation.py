"""Real LaunchServices launch through the gate sandbox (T48b).

Opt-in (--slow / ORCEXE_SLOW_TESTS=1): the control run starts a throwaway app outside
any sandbox and registers it with LaunchServices, a side effect on the host that the
default suite must not have. The profile text itself is checked in tests/unit.
"""

import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import time
import unittest

from tests.unit._timing import scaled
from orcexe import isolation


LSREGISTER = ('/System/Library/Frameworks/CoreServices.framework/Frameworks/'
              'LaunchServices.framework/Support/lsregister')


class LaunchServicesSandboxTests(unittest.TestCase):
    def test_real_sandbox_refuses_launchservices_open(self):
        if sys.platform != 'darwin' or not os.access('/usr/bin/open', os.X_OK):
            self.skipTest('LaunchServices (/usr/bin/open) exists only on macOS')
        if isolation.backend() != 'seatbelt':
            self.skipTest('sandbox-exec unavailable (missing, refused by host, or ORCEXE_NO_SANDBOX=1)')
        # Target choice: a throwaway background-only bundle whose executable writes
        # one marker file OUTSIDE the step's writable directories and exits at once.
        # A nonexistent bundle id would fail with or without the deny, proving
        # nothing. Here, if lsopen were allowed, LaunchServices would start the
        # probe outside the sandbox and the marker would appear (the control run
        # proves the host can launch it); the probe exits by itself, so even then
        # no app is left running. LSBackgroundOnly keeps it off the Dock.
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            checkout, outside = root / 'checkout', root / 'outside'
            checkout.mkdir()
            outside.mkdir()

            def probe(name):
                app = root / (name + '.app')
                (app / 'Contents/MacOS').mkdir(parents=True)
                (app / 'Contents/Info.plist').write_text(
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    '<plist version="1.0"><dict>'
                    '<key>CFBundleExecutable</key><string>probe</string>'
                    '<key>CFBundleIdentifier</key><string>dev.orcexe.test.t48b.' + name + '</string>'
                    '<key>CFBundlePackageType</key><string>APPL</string>'
                    '<key>LSBackgroundOnly</key><true/></dict></plist>\n')
                script = app / 'Contents/MacOS/probe'
                script.write_text('#!/bin/sh\necho launched > ' + shlex.quote(str(outside / name)) + '\n')
                script.chmod(0o755)
                if os.access(LSREGISTER, os.X_OK):  # drop the throwaway bundle from the LS database
                    self.addCleanup(subprocess.run, [LSREGISTER, '-u', str(app)], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
                return app

            def appeared(marker, seconds):
                deadline = time.monotonic() + scaled(seconds)
                while time.monotonic() < deadline:
                    if marker.exists():
                        return True
                    time.sleep(.05)
                return marker.exists()

            control = subprocess.run(['/usr/bin/open', '-g', '-n', str(probe('control'))],
                                     stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
            if control.returncode != 0 or not appeared(outside / 'control', 10):
                self.skipTest('host cannot launch apps through LaunchServices (headless session?)')
            result = isolation.run(['/usr/bin/open', '-g', '-n', str(probe('sandboxed'))],
                                   cwd=checkout, env=os.environ, timeout_s=30, stall_s=None)
            self.assertNotEqual(result.returncode, 0, result.stderr_tail)
            self.assertFalse(appeared(outside / 'sandboxed', 3), 'app launched outside the sandbox')



def load_tests(loader, standard_tests, pattern):
    if os.environ.get('ORCEXE_SLOW_TESTS') != '1':
        return unittest.TestSuite()
    return standard_tests
