"""Write isolation and control-state evidence without real coding tools."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from tests.unit.test_task import TaskFixture, APP, TEST
from tests.unit.test_planning import task as plan_task
from tests.unit.test_do import DoFixture
from orcexe import gate_lite, isolation


class IsolationTests(unittest.TestCase):
    def test_profile_canonical_paths_and_escaping(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            real = root / 'quote" slash\\ newline\n'
            real.mkdir()
            alias = root / 'alias'
            alias.symlink_to(real, target_is_directory=True)
            policy = isolation.profile(alias, root / 'tmp')
            self.assertIn('(deny file-write*)', policy)
            self.assertIn('(deny network-outbound)', policy)
            self.assertIn(isolation._quote(real), policy)
            self.assertNotIn(str(alias), policy)
            self.assertIn('quote\\" slash\\\\ newline\\n', policy)
            self.assertIn('(subpath "/dev/fd")', policy)
            if sys.platform == 'darwin':
                self.assertIn('/private/var/', isolation.profile('/var/tmp/checkout', '/var/tmp/scratch'))

    def test_profile_denies_launchservices_and_apple_events(self):
        # An app started through LaunchServices or driven by Apple events runs
        # outside the profile and could write anywhere after the step (T48b).
        policy = isolation.profile('/var/tmp/checkout', '/var/tmp/scratch')
        self.assertIn('(deny lsopen)', policy)
        self.assertIn('(deny appleevent-send)', policy)
        self.assertIn('(allow default)', policy)

    def test_real_sandbox_denies_outside_writes_including_children_and_symlinks(self):
        if isolation.backend() == 'none':
            # Measured, not assumed per platform: the probe creates a real sandbox (T63b).
            self.skipTest('OS sandbox unavailable: ' + isolation.unavailable_reason())
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw).resolve()
            checkout = root / 'checkout'
            checkout.mkdir()
            (checkout / 'escape').symlink_to(root, target_is_directory=True)
            script = ('import errno,os,sys,subprocess\nfrom pathlib import Path\n'
                      'Path("inside").write_text("yes")\n'
                      'Path(os.environ["TMPDIR"],"scratch").write_text("yes")\n'
                      'assert os.environ["TMPDIR"] == os.environ["TEMP"] == os.environ["TMP"]\n'
                      'for target in [sys.argv[1], "escape/symlink-write"]:\n'
                      '    try: Path(target).write_text("no")\n'
                      # Seatbelt denies with EPERM; a bwrap read-only bind gives EROFS.
                      '    except OSError as error:\n'
                      '        if error.errno not in (errno.EPERM, errno.EACCES, errno.EROFS): raise\n'
                      '    else: raise AssertionError("outside write succeeded")\n'
                      'child=subprocess.run([sys.executable,"-c",'
                      '"from pathlib import Path; Path("+repr(sys.argv[1])+ ").write_text(\'no\')"],capture_output=True)\n'
                      'assert child.returncode != 0\n')
            result = isolation.run([sys.executable, '-c', script, str(root / 'outside')],
                                   cwd=checkout, env=os.environ, timeout_s=10, stall_s=None)
            self.assertEqual(result.returncode, 0, result.stderr_tail)
            self.assertTrue((checkout / 'inside').exists())
            self.assertFalse((root / 'outside').exists())
            self.assertFalse((root / 'symlink-write').exists())

    def test_bwrap_argv_has_read_only_root_and_private_network(self):
        with tempfile.TemporaryDirectory() as raw, mock.patch.object(isolation, 'backend', return_value='bwrap'), \
                mock.patch.object(isolation.shutil, 'which', return_value='/fake/bwrap'), \
                mock.patch.object(isolation.proc, 'run') as run:
            isolation.run(['runner'], cwd=raw, env={})
            argv = run.call_args.args[0]
            self.assertEqual(argv[:5], ['/fake/bwrap', '--die-with-parent', '--ro-bind', '/', '/'])
            self.assertIn('--unshare-net', argv)
            self.assertEqual(argv[-2:], ['--', 'runner'])
            self.assertEqual(argv.count('--bind'), 2)

    def probe(self, platform, returncode=0, stderr=b'', which='/fake/bwrap', error=None):
        isolation._probe.cache_clear()
        self.addCleanup(isolation._probe.cache_clear)
        done = mock.Mock(returncode=returncode, stderr=stderr)
        with mock.patch.object(isolation.sys, 'platform', platform), \
                mock.patch.object(isolation.shutil, 'which', return_value=which), \
                mock.patch.object(isolation.os, 'access', return_value=platform == 'darwin'), \
                mock.patch.object(isolation.subprocess, 'run', return_value=done,
                                  side_effect=error) as run, \
                mock.patch.dict(os.environ, {'ORCEXE_NO_SANDBOX': ''}):
            return isolation.backend(), isolation.unavailable_reason(), run

    def test_bwrap_probe_uses_the_run_sandbox_shape(self):
        mode, reason, run = self.probe('linux')
        self.assertEqual((mode, reason), ('bwrap', ''))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:5], ['/fake/bwrap', '--die-with-parent', '--ro-bind', '/', '/'])
        for flag in ('--dev', '--unshare-net', '--chdir'):
            self.assertIn(flag, argv)
        self.assertEqual(argv.count('--bind'), 2)

    def test_namespace_denied_by_host_reports_unavailable_with_reason(self):
        # Ubuntu 24.04 AppArmor (unprivileged user namespaces restricted) and
        # refused mounts make bwrap exit 1 before the command runs.
        for stderr in (b'bwrap: setting up uid map: Permission denied\n',
                       b'bwrap: No permissions to creating new namespace\n',
                       b"bwrap: Can't mount devpts on /newroot/dev/pts: Permission denied\n"):
            with self.subTest(stderr=stderr):
                mode, reason, _ = self.probe('linux', 1, stderr)
                self.assertEqual(mode, 'none')
                self.assertIn('bwrap cannot create a sandbox on this host (exit 1)', reason)
                self.assertIn(stderr.decode().split(':', 1)[1].strip()[:20], reason)
        mode, reason, _ = self.probe('linux', error=subprocess.TimeoutExpired('bwrap', 5))
        self.assertEqual((mode, reason), ('none', 'bwrap probe failed: TimeoutExpired'))
        mode, reason, run = self.probe('linux', which=None)
        self.assertEqual(mode, 'none')
        self.assertIn('no OS sandbox backend', reason)
        run.assert_not_called()

    def test_seatbelt_probe_uses_the_run_profile(self):
        mode, reason, run = self.probe('darwin')
        self.assertEqual((mode, reason), ('seatbelt', ''))
        argv = run.call_args.args[0]
        self.assertEqual(argv[:2], ['/usr/bin/sandbox-exec', '-p'])
        self.assertIn('(deny file-write*)', argv[2])

    def test_bwrap_setup_failure_at_run_time_records_unavailable(self):
        refused = isolation.proc.ProcResult('exited', 1, 0.0, 0, 0, 0,
                                            'bwrap: setting up uid map: Permission denied\n',
                                            False, None)
        child_failed = isolation.proc.ProcResult('exited', 1, 0.0, 0, 0, 0, 'Traceback ...\n',
                                                 False, None)
        for result, expected in ((refused, 'none'), (child_failed, 'bwrap')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as raw, \
                    mock.patch.object(isolation, 'backend', return_value='bwrap'), \
                    mock.patch.object(isolation.shutil, 'which', return_value='/fake/bwrap'), \
                    mock.patch.object(isolation, 'verify'), \
                    mock.patch.object(isolation, 'snapshot', return_value={}), \
                    mock.patch.object(isolation.proc, 'run', return_value=result):
                session = isolation.Session(raw)
                token = isolation._active.set(session)
                try:
                    self.assertIs(isolation.run(['runner'], cwd=raw, env={}), result)
                finally:
                    isolation._active.reset(token)
                self.assertEqual(session.mode, expected)


class TaskIsolationTests(TaskFixture):
    def test_no_sandbox_runs_but_never_accepts_and_records_none(self):
        self.env['ORCEXE_NO_SANDBOX'] = '1'
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state'], result['reason'], result['attempts']),
                         (0, 'gate_passed', 'isolation_unavailable', 1), result)
        event = list(self.log().records(event='gate.passed'))[-1]
        self.assertEqual(event['data']['isolation'], 'none')
        self.assertTrue((Path(self.requests[0].run_dir) / 'l0/result.json').exists())

    def test_fast_no_sandbox_is_gate_passed(self):
        self.env['ORCEXE_NO_SANDBOX'] = '1'
        self.saved = self.save([plan_task()])
        self.actions.write_text(json.dumps([{'README.md': 'Example corrected\n'}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state'], result['reason']),
                         (0, 'gate_passed', 'isolation_unavailable'), result)

    def test_ledger_and_control_tamper_are_terminal_and_block_future_dependencies(self):
        for target in ('ledger.jsonl', 'plan.json', 'decisions.jsonl', 'runs/status.md'):
            with self.subTest(target=target):
                # Recreate state after each terminal probe; only temporary fixture files.
                marker = self.main / '.orcexe/state-tampered'
                if marker.exists():
                    marker.unlink()
                self.log().path.unlink(missing_ok=True)
                # T55: deleting the ledger is itself detected by its head ref; reset both.
                self.git_run(self.main, 'update-ref', '-d', 'refs/orcexe/ledger-head')
                self.saved = self.save()
                self.env['ORCEXE_NO_SANDBOX'] = '1'
                destination = self.main / '.orcexe' / target
                payload = ('\nfrom pathlib import Path\n'
                           'Path(' + repr(str(destination)) + ').write_text("forged")\n')
                self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
                    'tests/test_app.py': TEST.replace('1', '2') + payload}]))
                code, result, _ = self.invoke()
                self.assertEqual((code, result['state'], result['reason'], result['attempts']),
                                 (1, 'rejected', 'state_tampered', 1), result)
                self.assertIsNone(result['land'])
                self.assertTrue(marker.exists())
                # Even a now-valid plan/ledger cannot reuse the corrupted state.
                self.log().path.unlink(missing_ok=True)
                # T59: the deleted ledger cannot take the approval anchor; the marker decides first.
                self.saved = self.save(anchor=False)
                code, result, _ = self.invoke()
                self.assertEqual((code, result['reason'], result['attempts']), (1, 'state_tampered', 0))

    def test_valid_chain_forgery_cannot_approve_a_dependency(self):
        self.env['ORCEXE_NO_SANDBOX'] = '1'
        target = self.log().path
        payload = ('\nimport json,hashlib\nfrom pathlib import Path\n'
                   'ledger_path=Path(' + repr(str(target)) + ')\n'
                   'lines=ledger_path.read_bytes().splitlines()\n'
                   'record=json.loads(lines[-1])\n'
                   'record.update(seq=record["seq"]+1,event="task.state",task="T2",'
                   'data={"from":"draft","to":"accepted","reason":"forged"},'
                   'prev=hashlib.sha256(lines[-1]).hexdigest())\n'
                   'with ledger_path.open("ab") as handle: handle.write(json.dumps(record).encode()+b"\\n")\n')
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + payload}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state'], result['reason']), (1, 'rejected', 'state_tampered'), result)
        self.assertTrue(self.log().verify()['ok'])  # An unanchored hash chain alone accepts this forgery.
        self.assertTrue(list(self.log().records(task='T2')))
        # T59: the forged tail refuses the approval anchor; the marker decides first.
        self.saved = self.save([plan_task(id='T2'), plan_task(id='T3', depends_on=['T2'])], anchor=False)
        code, result, _ = self.invoke(task_id='T3')
        self.assertEqual((code, result['reason'], result['attempts']), (1, 'state_tampered', 0), result)

    def test_base_and_red_steps_also_detect_control_tamper(self):
        self.env['ORCEXE_NO_SANDBOX'] = '1'
        destination = self.main / '.orcexe/status.md'
        payload = ('\nfrom pathlib import Path\nif app() == 1:\n    Path('
                   + repr(str(destination)) + ').write_text("forged")\n')
        # Candidate is clean; only red-first gets this newly added payload.
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + payload}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['reason']), (1, 'state_tampered'), result)
        (self.main / '.orcexe/state-tampered').unlink()
        destination.unlink()
        # Now base counting itself sees the control-writing baseline code.
        baseline = self.main / 'tests/test_app.py'
        baseline.write_text(TEST + payload)
        self.git_run(self.main, 'add', 'tests/test_app.py')
        self.git_run(self.main, 'commit', '-qm', 'base control probe')
        self.head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        self.saved = self.save()
        code, result, _ = self.invoke()
        self.assertEqual((code, result['reason']), (1, 'state_tampered'), result)

    def test_invalid_ledger_refuses_dependency_read(self):
        self.log().path.write_text('forged\n')
        code, result, err = self.invoke()
        # T55: an unreadable ledger is ledger_corrupt (exit 2) with a repair line.
        self.assertEqual((code, result['reason'], result['attempts']), (2, 'ledger_corrupt', 0))
        self.assertIn('orcexe ledger repair', err)

    def test_fast_risk_touch_is_terminal_before_gates(self):
        self.saved = self.save([plan_task()])
        self.actions.write_text(json.dumps([{'README.md': 'Example corrected\n'}]))
        with mock.patch('orcexe.task.risk_areas.touched_paths', return_value=['README.md']), \
                mock.patch.object(gate_lite, 'gate') as gate:
            code, result, _ = self.invoke()
        self.assertEqual((code, result['state'], result['reason'], result['attempts']),
                         (1, 'rejected', 'risk_path_touched', 1), result)
        gate.assert_not_called()

    def test_sandbox_denial_is_test_failure_with_feedback_hint(self):
        if isolation.backend() == 'none':
            self.skipTest('OS sandbox unavailable (host denies sandbox creation)')
        victim = self.directory / 'outside-checkout'
        self.actions.write_text(json.dumps([{'src/app.py': APP.replace('1', '2'),
            'tests/test_app.py': TEST.replace('1', '2') + '\nfrom pathlib import Path\nPath('
            + repr(str(victim)) + ').write_text("no")\n'}]))
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (1, 'exhausted'), result)
        self.assertFalse(victim.exists())
        self.assertIn('write outside checkout denied', self.briefs[1])

    def test_all_code_steps_snapshot_controls_and_use_sandbox_wrapper(self):
        real = isolation.run
        calls = []
        def run(argv, **kwargs):
            calls.append((Path(kwargs['cwd']), isolation._active.get().repo))
            return real(argv, **kwargs)
        with mock.patch.object(isolation, 'run', side_effect=run):
            code, result, _ = self.invoke()
        self.assertEqual(code, 0, result)
        self.assertEqual(len(calls), 4)  # interpreter probe, candidate, base, changed tests on base
        self.assertEqual({repo for _, repo in calls}, {self.main})
        shapes = [(len(path.parts), path.name, path.parent.parent) for path, _ in calls]
        self.assertEqual(shapes, [shapes[0]] * 4)
        self.assertEqual(len({path for path, _ in calls}), 3)
        self.assertFalse(any(part in ('red', 'base') for path, _ in calls for part in path.parts))


class DoIsolationTests(DoFixture):
    def test_no_sandbox_never_accepts(self):
        self.env['ORCEXE_NO_SANDBOX'] = '1'
        result = self.invoke()
        self.assertEqual((result['verdict'], result['reason']), ('unverified', 'isolation_unavailable'), result)
        self.assertNotIn('land_commands', result)
        self.assertEqual(list(self.log().records(event='gate.result'))[-1]['data']['isolation'], 'none')

    def test_control_tamper_is_state_tampered(self):
        self.env['ORCEXE_NO_SANDBOX'] = '1'
        victim = self.repo / '.orcexe/status.md'
        test = self.repo / 'tests/test_app.py'
        test.write_text(test.read_text() + '\nfrom pathlib import Path\nPath('
                        + repr(str(victim)) + ').write_text("forged")\n')
        self.git('add', 'tests/test_app.py')
        self.git('commit', '-qm', 'control write regression')
        result = self.invoke()
        self.assertEqual((result['verdict'], result['reason']), ('failed', 'state_tampered'), result)
        self.assertNotIn('land_commands', result)

    def test_corrupted_ledger_does_not_mask_terminal_tamper_reason(self):
        self.env['ORCEXE_NO_SANDBOX'] = '1'
        victim = self.repo / '.orcexe/ledger.jsonl'
        test = self.repo / 'tests/test_app.py'
        test.write_text(test.read_text() + '\nfrom pathlib import Path\nPath('
                        + repr(str(victim)) + ').write_text("forged")\n')
        self.git('add', 'tests/test_app.py')
        self.git('commit', '-qm', 'ledger write regression')
        result = self.invoke()
        self.assertEqual((result['verdict'], result['reason']), ('failed', 'state_tampered'), result)
        self.assertNotIn('land_commands', result)
