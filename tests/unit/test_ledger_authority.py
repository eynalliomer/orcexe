"""T55: ledger authority -- run-time writes are tamper, a CAS head ref, a repairable tail."""

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.unit.test_gitutil import GitFixture
from tests.unit.test_planning import task as plan_task
from tests.unit.test_task import TaskFixture
from tests.unit import test_task_l1
from orcexe import cli, do, gate_lite, isolation, ledger, runguard, task, task_gate
from orcexe.adapters import ClaudeCodeAdapter


# In the past: other processes (the forger, the CLI) append with the real clock after it.
STAMP = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
STATE = {'from': 'running', 'to': 'accepted', 'reason': 'forged'}
SOURCE = str(Path(__file__).resolve().parents[2] / 'plugins/orcexe/src')
FORGER = ('import sys\nsys.path.insert(0, sys.argv[1])\nfrom orcexe import ledger\n'
          'ledger.Ledger(sys.argv[2], anchor=sys.argv[3] == "1").append("task.state", sys.argv[5], '
          'dict(plan_id=sys.argv[4], **{"from": "running", "to": "accepted", "reason": "forged"}))\n')


def forge(env, path, plan_id='p', raw=False, task_id='T3'):
    """A different process (for example an executor's leftover child) appends a chain-valid record.

    raw=False also moves the head ref, as orcexe's own writer does; raw=True leaves it behind.
    """
    subprocess.run([sys.executable, '-c', FORGER, SOURCE, str(path), '0' if raw else '1', plan_id, task_id],
                   env=env, check=True, capture_output=True, timeout=60)


def run_cli(env, *arguments):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(arguments))
    return code, out.getvalue(), err.getvalue()


class HeadAnchorTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.path = ledger.default_path(self.main)
        self.log = ledger.Ledger(self.path, clock=lambda: STAMP)
        self.patcher = mock.patch.dict(os.environ, self.env, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def append(self, count=3, log=None):
        for index in range(count):
            (log or self.log).append('task.state', 'T' + str(index), STATE)

    def lines(self):
        return self.path.read_bytes().splitlines(keepends=True)

    def ref(self):
        result = subprocess.run([self.git, 'for-each-ref', '--format=%(objectname)', ledger.HEAD_REF],
                                cwd=str(self.main), env=self.env, capture_output=True, text=True, check=True)
        return result.stdout.strip() or None

    def delete_ref(self):
        subprocess.run([self.git, 'update-ref', '-d', ledger.HEAD_REF], cwd=str(self.main), env=self.env, check=True)

    def cli(self, *arguments):
        return run_cli(self.env, 'ledger', arguments[0], '--repo', str(self.main), *arguments[1:])

    def test_every_append_moves_the_head_ref_and_anchoring_is_recorded(self):
        self.assertIsNone(self.ref())
        self.append(1)
        first = self.ref()
        self.assertIsNotNone(first)
        self.append(1)
        self.assertNotEqual(self.ref(), first)
        # seq 1 records the anchoring itself; it is hidden from ordinary readers.
        events = [r['event'] for r in self.log.records(include_meta=True)]
        self.assertEqual(events, ['ledger.anchored', 'task.state', 'task.state'])
        self.assertEqual([r['event'] for r in self.log.records()], ['task.state', 'task.state'])
        self.assertEqual(self.log.verify_head(), dict(records=3, last_seq=3, ok=True, anchored=True, adopted=False))
        blob = subprocess.run([self.git, 'cat-file', '-p', ledger.HEAD_REF], cwd=str(self.main), env=self.env,
                              capture_output=True, text=True, check=True).stdout
        self.assertTrue(blob.startswith('orcexe.ledger.head.v1\nseq 3\nsha256 '), blob)
        code, out, _ = self.cli('verify')
        self.assertEqual(code, 0)
        self.assertIn('Ledger OK: 3 records; head anchored.', out)

    def test_suffix_deletion_and_last_line_edit_are_detected(self):
        self.append(3)
        original = self.path.read_bytes()
        lines = self.lines()
        edited = lines[-1].replace(b'"forged"', b'"edited"')
        for name, data in (('suffix_deleted', b''.join(lines[:-1])), ('last_line_edited', b''.join(lines[:-1]) + edited),
                           ('all_deleted', b'')):
            with self.subTest(name):
                self.path.write_bytes(data)
                self.log.verify()  # The chain alone still verifies: only the external head sees it.
                with self.assertRaises(ledger.HeadMismatch) as caught:
                    self.log.verify_head()
                self.assertEqual(ledger.reason(caught.exception), 'ledger_head_mismatch')
                with self.assertRaises(ledger.HeadMismatch):
                    self.log.append('task.state', 'T9', STATE)
                self.assertEqual(self.path.read_bytes(), data, 'a refused append writes nothing')
                code, out, _ = self.cli('verify')
                self.assertEqual(code, 2)
                self.assertIn('not_seen: ledger_head_mismatch', out)
                self.assertIn('says seq 4', out)
                self.assertIn('orcexe ledger repair', out)
        self.path.unlink()
        with self.assertRaises(ledger.HeadMismatch):
            self.log.verify_head()
        self.path.write_bytes(original)
        self.assertTrue(self.log.verify_head()['ok'])

    def test_old_unanchored_ledger_is_adopted_once_and_a_deleted_ref_is_detected(self):
        self.append(2, ledger.Ledger(self.path, clock=lambda: STAMP, anchor=False))
        self.assertIsNone(self.ref())
        result = self.log.verify_head()
        self.assertEqual((result['adopted'], result['records']), (True, 3))
        self.assertEqual(list(self.log.records(include_meta=True))[-1]['event'], 'ledger.anchored')
        anchored = self.ref()
        self.assertFalse(self.log.verify_head()['adopted'])
        self.assertEqual(self.ref(), anchored)
        # P3-1: the ledger itself says it was anchored, so a deleted ref is never re-adopted,
        # also after a truncation that keeps the anchoring record.
        self.append(2)
        self.delete_ref()
        self.path.write_bytes(b''.join(self.lines()[:3]))
        for check in (self.log.verify_head, lambda: self.log.append('task.state', 'T9', STATE)):
            with self.assertRaises(ledger.HeadMismatch) as caught:
                check()
            self.assertEqual(caught.exception.detail, 'ref_deleted')
        self.assertIsNone(self.ref())
        code, out, _ = self.cli('verify')
        self.assertEqual(code, 2)
        self.assertIn('ref_deleted', out)

    def test_unanchored_ledger_with_a_bad_chain_is_never_adopted(self):
        self.append(2, ledger.Ledger(self.path, clock=lambda: STAMP, anchor=False))
        lines = self.lines()
        self.path.write_bytes(lines[0].replace(b'"T0"', b'"TX"') + b''.join(lines[1:]))
        with self.assertRaises(ledger.LedgerError) as caught:
            self.log.append('task.state', 'T9', STATE)
        self.assertNotIsInstance(caught.exception, ledger.HeadMismatch)
        self.assertIsNone(self.ref())

    def test_non_repository_and_explicit_opt_out_are_not_anchored(self):
        plain = ledger.Ledger(self.directory / 'elsewhere' / '.orcexe' / 'ledger.jsonl', clock=lambda: STAMP)
        plain.append('task.state', 'T1', STATE)
        self.assertEqual(plain.verify_head()['anchored'], False)
        self.assertEqual([r['event'] for r in plain.records(include_meta=True)], ['task.state'])
        self.append(1, ledger.Ledger(self.path, clock=lambda: STAMP, anchor=False))
        self.assertIsNone(self.ref())

    def test_cas_race_refuses_without_overwriting_and_leaves_no_line(self):
        self.append(1)
        before = self.path.read_bytes()
        real = ledger._git
        foreign = []

        def racing(repo, *args, **kwargs):
            if args and args[0] == 'update-ref' and not foreign:
                # Someone else moves the ref between this append's read and its update.
                blob = real(repo, 'hash-object', '-w', '--stdin', data=b'foreign\n')[1].decode().strip()
                foreign.append(blob)
                self.assertEqual(real(repo, 'update-ref', ledger.HEAD_REF, blob)[0], 0)
            return real(repo, *args, **kwargs)

        with mock.patch.object(ledger, '_git', side_effect=racing):
            with self.assertRaises(ledger.HeadMismatch) as caught:
                self.log.append('task.state', 'T1', STATE)
        self.assertEqual(caught.exception.detail, 'head_moved')
        self.assertEqual(self.ref(), foreign[0], 'the foreign ref value is never overwritten')
        self.assertEqual(self.path.read_bytes(), before, 'P3-4: the refused line is rolled back')

    def test_corrupt_tail_reports_exit_2_and_repair_quarantines_it(self):
        self.append(3)
        good = self.path.read_bytes()
        junk = b'{"schema": "orcexe.ledger.v1", "seq": 5, "torn'
        with self.path.open('ab') as handle:
            handle.write(junk)
        with self.assertRaises(ledger.LedgerError) as caught:
            self.log.verify_head()
        self.assertEqual(ledger.reason(caught.exception), 'ledger_corrupt')
        code, out, _ = self.cli('verify')
        self.assertEqual(code, 2)
        self.assertIn('not_seen: ledger_corrupt', out)
        self.assertIn('orcexe ledger repair', out)
        code, out, _ = self.cli('repair')
        self.assertEqual(code, 0, out)
        self.assertIn('Moved 1 line(s) / {} byte(s) from line 5 on (truncated_tail)'.format(len(junk)), out)
        quarantined = list(self.path.parent.glob('ledger.quarantine-*.jsonl'))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), junk)
        self.assertEqual(stat.S_IMODE(quarantined[0].stat().st_mode), 0o600)
        self.assertEqual(self.path.read_bytes(), good)
        self.assertEqual(self.cli('verify')[0], 0)
        self.append(1)
        self.assertEqual(self.log.verify_head()['records'], 5)

    def test_mid_file_corruption_moves_the_rest_byte_for_byte(self):
        self.append(4)
        lines = self.lines()
        bad = lines[2].replace(b'"prev": "', b'"prev": "f', 1)
        self.path.write_bytes(b''.join(lines[:2]) + bad + b''.join(lines[3:]))
        with self.assertRaises(ledger.RepairRefused):
            self.log.repair(now=STAMP)  # the head ref names seq 5, which is gone with the bad line
        report = self.log.repair(now=STAMP, accept_unanchored=True)
        self.assertEqual((report['moved']['first_line'], report['moved']['lines']), (3, 3))
        self.assertTrue(report['reanchored'])
        self.assertEqual(Path(report['moved']['path']).read_bytes(), bad + b''.join(lines[3:]))
        self.assertEqual(Path(report['moved']['path']).name, 'ledger.quarantine-20260101T120000Z.jsonl')
        self.assertEqual(self.path.read_bytes(), b''.join(lines[:2]))
        self.assertEqual(self.log.verify_head()['records'], 2)
        with self.path.open('ab') as handle:
            handle.write(b'junk\n')
        second = self.log.repair(now=STAMP)  # a torn tail past the head ref is quarantined by default
        self.assertNotEqual(second['moved']['path'], report['moved']['path'])
        self.assertEqual(len(list(self.path.parent.glob('ledger.quarantine-*.jsonl'))), 2)

    def test_repair_never_changes_a_valid_ledger(self):
        self.append(2)
        before, ref = self.path.read_bytes(), self.ref()
        code, out, _ = self.cli('repair')
        self.assertEqual(code, 0)
        self.assertIn('Nothing to repair', out)
        self.assertEqual((self.path.read_bytes(), self.ref()), (before, ref))
        self.assertEqual(list(self.path.parent.glob('ledger.quarantine-*')), [])

    def test_records_past_the_head_ref_are_quarantined_and_shown_never_anchored_silently(self):
        self.append(2)
        forge(self.env, self.path, raw=True)  # another process: chain-valid, ref left behind
        code, out, _ = self.cli('repair')
        self.assertEqual(code, 0, out)
        self.assertIn('quarantined: seq 4', out)
        self.assertIn('T3 task.state to=accepted reason=forged', out)
        self.assertEqual(len(self.lines()), 3)
        self.assertEqual(self.cli('verify')[0], 0)
        # With an explicit, reviewed opt-in the extra record is anchored instead.
        forge(self.env, self.path, raw=True)
        code, out, _ = self.cli('repair', '--accept-unanchored')
        self.assertEqual(code, 0, out)
        self.assertEqual(len(self.lines()), 4)
        self.assertEqual(self.cli('verify')[0], 0)

    def test_suffix_deletion_repair_refuses_without_the_opt_in(self):
        self.append(3)
        lines = self.lines()
        self.path.write_bytes(b''.join(lines[:2]))
        code, out, _ = self.cli('repair')
        self.assertEqual(code, 2, out)
        self.assertIn('Repair refused: the head ref names no record of this ledger', out)
        self.assertEqual(self.path.read_bytes(), b''.join(lines[:2]))
        code, out, _ = self.cli('repair', '--json', '--accept-unanchored')
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual((report['head_found'][0], report['head_now'][0], report['reanchored']), (4, 2, True))
        self.assertIsNone(report['moved'])
        self.assertEqual(self.cli('verify')[0], 0)

    def test_marker_trust_point_bounds_repair_and_verify_reports_tamper(self):
        self.append(2)
        with ledger.session():
            self.log.verify_head()
            trusted = ledger.trusted(self.path)
        forge(self.env, self.path)          # ref moved too: the chain and head agree
        ledger.write_marker(self.path.parent, trusted)
        code, out, _ = self.cli('verify')
        self.assertEqual(code, 2)
        self.assertIn('not_seen: state_tampered', out)
        self.assertIn('last trusted record: seq 3', out)
        code, out, _ = self.cli('repair')
        self.assertEqual(code, 0, out)
        self.assertIn('quarantined: seq 4', out)
        self.assertIn('The marker stays', out)
        self.assertEqual(len(self.lines()), 3)
        (self.path.parent / ledger.MARKER).unlink()
        self.assertEqual(self.cli('verify')[0], 0)

    def test_marker_without_a_trust_point_never_anchors_by_itself(self):
        self.append(2, ledger.Ledger(self.path, clock=lambda: STAMP, anchor=False))
        (self.path.parent / ledger.MARKER).write_text('state_tampered\n')
        code, out, _ = self.cli('repair')
        self.assertEqual(code, 2, out)
        self.assertIn('without a trusted point', out)
        self.assertIsNone(self.ref())
        self.assertEqual(self.cli('repair', '--accept-unanchored')[0], 0)
        self.assertIsNotNone(self.ref())

    def test_linked_ledger_is_refused_and_its_target_untouched(self):
        self.append(3)
        target = self.directory / 'real-ledger.jsonl'
        target.write_bytes(self.path.read_bytes() + b'{"torn')
        size = target.stat().st_size
        self.path.unlink()
        os.symlink(target, self.path)
        for command in ('verify', 'repair'):
            code, out, _ = self.cli(command)
            self.assertEqual(code, 2, out)
            self.assertIn('ledger_corrupt', out)
        with self.assertRaises(ledger.LedgerError):
            self.log.append('task.state', 'T9', STATE)
        self.assertEqual(target.stat().st_size, size)

    def test_session_trust_refuses_any_tail_this_command_did_not_write(self):
        self.append(1)
        with ledger.session():
            self.log.verify_head()
            self.log.append('task.state', 'T1', STATE)
            forge(self.env, self.path)  # chain-valid and the ref moved: only the trust sees it
            for read in (lambda: list(self.log.records()), self.log.verify_head,
                         lambda: self.log.append('task.state', 'T2', STATE)):
                with self.assertRaises(ledger.ForeignRecord):
                    read()
            self.assertEqual(ledger.reason(ledger.ForeignRecord()), 'state_tampered')
        present, point = ledger.read_marker(self.path.parent)
        self.assertTrue(present)
        self.assertEqual(point[0], 3)
        # Outside a command (no session) the same ledger reads normally.
        self.assertEqual(len(list(self.log.records())), 3)

    def test_cli_refuses_non_repositories(self):
        code, out, _ = run_cli(self.env, 'ledger', 'verify', '--repo', str(self.directory / 'absent'))
        self.assertEqual((code, out.strip()), (2, 'not_seen: not_a_repository'))


class RunGuardTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.patcher = mock.patch.dict(os.environ, self.env, clear=True)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.log = ledger.Ledger(ledger.default_path(self.main), clock=lambda: STAMP)
        self.log.append('task.state', 'T1', STATE)
        self.runs = self.main / '.orcexe/runs'
        self.root = self.runs / 'task-1'
        (self.root / 'attempt-1').mkdir(parents=True)
        (self.root / 'contract.lite.json').write_text('{}')
        (self.runs / 'task-0').mkdir()
        self.guard = runguard.Guard(self.main, self.runs, self.log)

    def raw_append(self, data):
        with self.log.path.open('ab') as handle:
            handle.write(data)

    def test_adapter_outputs_in_the_own_directory_are_not_tamper(self):
        own = self.root / 'attempt-1'

        def adapter():
            for name in ('.attempt', 'brief.md', 'final.md', 'events.jsonl', 'stderr.txt', 'result.json'):
                (own / name).write_text('x')
            return 'done'

        self.assertEqual(self.guard.run(adapter, own), 'done')
        # The CLI's own writes between calls (gate receipts, the L1 brief) are outside any window.
        (own / 'gate.json').write_text('{}')
        self.guard.run(lambda: (own / 'l1' / 'brief').mkdir(parents=True) or
                       (own / 'l1' / 'brief' / 'brief.md').write_text('b'), own / 'l1')
        self.guard.check()
        self.assertFalse((self.main / '.orcexe/state-tampered').exists())

    def test_foreign_writes_are_terminal_sticky_and_mark_the_trust_point(self):
        own = self.root / 'attempt-1'
        cases = {
            'ledger_append': lambda: forge(self.env, self.log.path),
            'ledger_raw': lambda: self.raw_append(b'x'),
            'ref_moved': lambda: subprocess.run([self.git, 'update-ref', '-d', ledger.HEAD_REF], cwd=str(self.main),
                                                env=self.env, check=True),
            'planted_in_own': lambda: (own / 'planted.txt').write_text('x'),
            'planted_at_root': lambda: (self.root / 'x.json').write_text('{}'),
            'other_dispatch': lambda: (self.runs / 'task-0' / 'contract.lite.json').write_text('{}'),
            'contract_edited': lambda: (self.root / 'contract.lite.json').write_text('{"x": 1}'),
            'evidence_removed': lambda: (self.root / 'contract.lite.json').unlink(),
            'output_as_directory': lambda: (own / 'final.md').mkdir(),
        }
        for name, evil in cases.items():
            with self.subTest(name):
                good = self.log.path.read_bytes()
                with ledger.session():
                    log = ledger.Ledger(self.log.path, clock=lambda: STAMP)
                    log.verify_head()
                    trusted = ledger.trusted(log.path)
                    guard = runguard.Guard(self.main, self.runs, log)
                    with self.assertRaises(isolation.StateTampered):
                        guard.run(evil, own)
                self.assertTrue(guard.tampered)
                with self.assertRaises(isolation.StateTampered):
                    guard.check()
                self.assertEqual(ledger.read_marker(self.main / '.orcexe'), (True, trusted))
                # Reset this fixture's state for the next case.
                (self.main / '.orcexe/state-tampered').unlink()
                self.log.path.write_bytes(good)
                self.log.repair(accept_unanchored=True)
                for path in (own / 'planted.txt', self.root / 'x.json', self.runs / 'task-0' / 'contract.lite.json'):
                    if path.exists():
                        path.unlink()
                if (own / 'final.md').is_dir():
                    (own / 'final.md').rmdir()
                (self.root / 'contract.lite.json').write_text('{}')

    def test_main_snapshot_change_is_main_repository_changed(self):
        state = {'value': 1}
        guard = runguard.Guard(self.main, self.runs, self.log, main_snapshot=lambda: state['value'])

        def change():
            state['value'] = 2

        with self.assertRaisesRegex(ValueError, 'main_repository_changed'):
            guard.run(change, self.root / 'attempt-1', with_main=True)
        with self.assertRaisesRegex(ValueError, 'main_repository_changed'):
            guard.check()
        self.assertFalse((self.main / '.orcexe/state-tampered').exists())

    def test_wrapped_adapter_flags_even_when_the_caller_swallows(self):
        class Adapter:
            manifest = 'm'

            def run(inner, req):
                (Path(req.run_dir) / 'planted').write_text('x')
                return 'receipt'

        wrapped = self.guard.wrap(Adapter())
        self.assertEqual(wrapped.manifest, 'm')
        try:
            wrapped.run(mock.Mock(run_dir=str(self.root / 'attempt-1')))
        except isolation.StateTampered:
            pass
        with self.assertRaises(isolation.StateTampered):
            self.guard.check()


class JunkLockTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix='orcexe-lock-')
        self.addCleanup(self.temporary.cleanup)
        self.lock = Path(self.temporary.name) / '.orcexe' / 'do.lock'
        self.lock.parent.mkdir()

    def age(self):
        old = time.time() - do.JUNK_LOCK_GRACE_S - 5
        os.utime(self.lock, (old, old))

    def test_old_empty_or_unparseable_lock_is_reclaimed_with_a_warning(self):
        for contents in (b'', b'   \n', b'{not json', b'[]', b'null'):
            with self.subTest(contents=contents):
                self.lock.write_bytes(contents)
                self.age()
                err = io.StringIO()
                with redirect_stderr(err):
                    info = do._acquire(self.lock, 'T1', lambda data: None)
                self.assertIsNotNone(info)
                self.assertIn('Warning: removed an empty or unreadable lock', err.getvalue())
                self.assertEqual(json.loads(self.lock.read_text())['task_id'], 'T1')
                self.lock.unlink()

    def test_objects_without_a_valid_owner_block_and_name_the_manual_step(self):
        # P3-2: a live owner's lock rewritten to {} must not be stealable.
        owner = do._acquire(self.lock, 'owner', lambda data: None)
        self.assertIsNotNone(owner)
        for contents in ('{}', '{"pid": null}', '{"pid": "x"}', '{"pid": 0}'):
            with self.subTest(contents=contents):
                self.lock.write_text(contents)
                self.age()
                err = io.StringIO()
                with redirect_stderr(err):
                    self.assertIsNone(do._acquire(self.lock, 'thief', lambda data: None))
                self.assertIn('names no valid owner pid', err.getvalue())
                self.assertIn('remove it by hand', err.getvalue())

    def test_fresh_junk_and_live_owner_locks_still_block(self):
        self.lock.write_bytes(b'')
        self.assertIsNone(do._acquire(self.lock, 'T1', lambda data: None))
        self.lock.write_text(json.dumps(dict(pid=os.getpid(), host=do.platform.node(), task_id='other')))
        self.age()
        self.assertIsNone(do._acquire(self.lock, 'T1', lambda data: None))


class GateLogMemoryTests(unittest.TestCase):
    def test_l0_reads_the_in_process_log_not_a_rewritten_file(self):
        with tempfile.TemporaryDirectory(prefix='orcexe-log-') as directory:
            gate = Path(directory) / 'gate'
            gate_lite._write_log(gate, 'unittest', 'Ran 2 tests in 0.01s\nOK\n')
            (gate / 'unittest.log').write_text('Ran 999 tests in 0.01s\nOK\n')
            self.assertEqual(gate_lite.project_log(gate, 'unittest'), 'Ran 2 tests in 0.01s\nOK\n')
            self.assertIsNone(gate_lite.project_log(gate, 'other'))


class AuthorityTaskTests(TaskFixture):
    evaluator = test_task_l1.EvaluatorTests.evaluator

    def forge(self, raw=False):
        """Another process appends a chain-valid record for task T3."""
        forge(self.env, ledger.default_path(self.main), self.saved['plan_id'], raw)

    def assert_tampered(self, code, result):
        self.assertEqual((code, result['state'], result['reason'], result['land']),
                         (1, 'rejected', 'state_tampered', None), result)
        self.assertTrue((self.main / '.orcexe/state-tampered').exists())
        self.assertEqual(self.git_run(self.main, 'for-each-ref', 'refs/orcexe/accepted/'), '')

    def three_tasks(self):
        tasks = [plan_task('T1', goal='Return two and update its test', lane='standard',
                           files={'allow': ['src/*.py', 'tests/**']}, done=['app returns 2 and its test passes']),
                 plan_task('T2', goal='Fix the README typo', depends_on=['T3']),
                 plan_task('T3', goal='Fix the README wording', files={'allow': ['README.md']})]
        self.saved = self.save(tasks)

    def ledger_events(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return [(r['task'], r['event'], r['data'].get('to')) for r in
                    ledger.Ledger(ledger.default_path(self.main)).records()]

    def test_normal_run_is_not_tamper_and_anchors_the_head(self):
        code, result, _ = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), result)
        self.assertFalse((self.main / '.orcexe/state-tampered').exists())
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertTrue(self.log().verify_head()['anchored'])

    def test_executor_time_forgery_is_state_tampered_and_recovery_quarantines_it(self):
        for raw in (False, True):
            with self.subTest(raw=raw):
                self.saved = self.save()
                for line in self.git_run(self.main, 'for-each-ref', '--format=%(refname)', 'refs/orcexe/accepted/').split():
                    self.git_run(self.main, 'update-ref', '-d', line)
                code, result, _ = self.invoke(after=lambda request: self.forge(raw))
                self.assert_tampered(code, result)
                # finish() never appends on top of the untrusted tail.
                self.assertEqual(self.ledger_events()[-1], ('T3', 'task.state', 'accepted'))
                code, result, err = self.invoke()
                self.assertEqual((code, result['reason'], result['attempts']), (1, 'state_tampered', 0), result)
                self.assertIn('Recovery:', err)
                # P2-2: status does not report the forged state as fact.
                code, out, err = run_cli(self.env, 'status', '--repo', str(self.main))
                self.assertEqual(code, 2)
                self.assertIn('not_seen: state_tampered', err)
                self.assertNotIn('accepted', out)
                # Repair stops at the marker's trusted record and shows what it moves.
                code, out, _ = run_cli(self.env, 'ledger', 'repair', '--repo', str(self.main))
                self.assertEqual(code, 0, out)
                self.assertIn('T3 task.state to=accepted reason=forged', out)
                self.assertNotIn(('T3', 'task.state', 'accepted'), self.ledger_events())
                (self.main / '.orcexe/state-tampered').unlink()
                code, out, err = run_cli(self.env, 'status', '--repo', str(self.main), '--json')
                self.assertEqual(code, 0, err)
                states = {t['task_id']: t['state'] for t in json.loads(out)['tasks']}
                self.assertEqual(states['T1'], 'running')
                # The attempt is resumable; finish it for the next subtest.
                with mock.patch.dict(os.environ, self.env, clear=True):
                    task.run_task(self.saved['plan_id'], 'T1', self.main, timeout=30, _resume=True)

    def test_planted_files_under_the_run_directories_are_state_tampered(self):
        targets = (lambda request: Path(request.run_dir) / 'planted.json',
                   lambda request: Path(request.run_dir).parent / 'contract.lite.json',
                   lambda request: Path(request.run_dir).parents[1] / 'task-other' / 'contract.lite.json')
        for index, target in enumerate(targets):
            with self.subTest(index=index):
                self.saved = self.save()

                def plant(request):
                    target(request).parent.mkdir(parents=True, exist_ok=True)
                    target(request).write_text('{}')

                code, result, _ = self.invoke(after=plant)
                self.assert_tampered(code, result)
                (self.main / '.orcexe/state-tampered').unlink()

    def test_l1_time_forged_record_is_state_tampered(self):
        self.evaluator([test_task_l1.rubric()])
        real = ClaudeCodeAdapter.run

        def run(adapter, req):
            self.forge()
            return real(adapter, req)

        with mock.patch.object(ClaudeCodeAdapter, 'run', new=run):
            code, result, _ = self.invoke()
        self.assert_tampered(code, result)
        self.assertIsNone(result['l1'])
        events = [e[1] for e in self.ledger_events()]
        self.assertIn('l1.started', events)
        self.assertNotIn('l1.verdict', events)

    def test_l1_verdict_comes_from_memory_not_a_rewritten_final_file(self):
        # P1-1, deterministic: final.md is rewritten after the evaluator wrote it.
        self.evaluator([test_task_l1.rubric(passed=False)])
        real = ClaudeCodeAdapter.run

        def run(adapter, req):
            receipt = real(adapter, req)
            Path(req.run_dir, 'final.md').write_text(json.dumps(test_task_l1.rubric(passed=True)))
            return receipt

        with mock.patch.object(ClaudeCodeAdapter, 'run', new=run):
            code, result, _ = self.invoke()
        self.assertNotEqual(result['state'], 'accepted', result)
        verdicts = [r['data']['criteria'] for r in self.log().records(event='l1.verdict')]
        self.assertTrue(verdicts)
        self.assertTrue(all(v == ['d1:fail'] for v in verdicts), verdicts)

    def test_surviving_executor_child_cannot_turn_an_l1_rejection_into_acceptance(self):
        # P1-1, real processes: the executor leaves a setsid grandchild that rewrites every
        # l1/final.md it can find while L1 runs (the reviewer's probe D).
        self.evaluator([test_task_l1.rubric(passed=False)])
        daemon = self.directory / 'daemon.py'
        stop = self.directory / 'daemon.stop'
        daemon.write_text(
            'import glob,json,os,sys,time\n'
            'if os.fork(): os._exit(0)\n'
            'os.setsid()\n'
            'if os.fork(): os._exit(0)\n'
            'for fd in (0,1,2): os.close(fd)\n'
            'good=json.dumps(' + repr(test_task_l1.rubric(passed=True)) + ')\n'
            'end=time.time()+20\n'
            'while time.time()<end and not os.path.exists(' + repr(str(stop)) + '):\n'
            '    for p in glob.glob(' + repr(str(self.main)) + '+"/.orcexe/runs/*/attempt-*/l1/final.md"):\n'
            '        try:\n'
            '            if open(p).read()!=good: open(p,"w").write(good)\n'
            '        except OSError: pass\n'
            '    time.sleep(0.0005)\n')
        self.addCleanup(stop.write_text, 'stop')
        wrapper = self.directory / 'fake-codex'
        text = wrapper.read_text()
        marker = 'if "exec" in sys.argv:\n'
        wrapper.write_text(text.replace(marker, marker + '    import subprocess\n    subprocess.run([sys.executable, '
                                        + repr(str(daemon)) + '])\n', 1))
        code, result, _ = self.invoke()
        self.assertNotEqual(result['state'], 'accepted', result)
        self.assertIsNone(result['land'])
        verdicts = [r['data']['criteria'] for r in self.log().records(event='l1.verdict')]
        self.assertTrue(verdicts and all(v == ['d1:fail'] for v in verdicts), verdicts)

    def test_between_window_forgery_with_the_ref_moved_is_state_tampered(self):
        # P2-1 (the reviewer's probe B): written after the executor window, before the gate.
        self.three_tasks()
        real = task_gate.observe
        done = []

        def observe(*args, **kwargs):
            if not done:
                done.append(1)
                self.forge()
            return real(*args, **kwargs)

        with mock.patch.object(task_gate, 'observe', new=observe):
            code, out, err = self.plan_run()
        result = json.loads(out)
        self.assertEqual((code, result['reason']), (2, 'state_tampered'), (result, err))
        self.assertEqual([r.task for r in self.requests], ['T1'], 'T2 is never dispatched')
        present, point = ledger.read_marker(self.main / '.orcexe')
        self.assertTrue(present)
        self.assertIsNotNone(point)

    def test_plan_run_stops_and_never_dispatches_dependants_of_a_forged_acceptance(self):
        self.three_tasks()
        code, out, err = self.plan_run(after=lambda request: self.forge())
        result = json.loads(out)
        self.assertEqual((code, result['reason']), (2, 'state_tampered'), (result, err))
        self.assertEqual(json.loads(self.counter.read_text()), 1, 'only T1 reached an executor')
        self.assertEqual(len(self.requests), 1)

    def test_l1_time_main_state_writes_are_refused(self):
        # P2-3: the L1 window sees the same main-repository state the executor window does.
        def decisions():
            with (self.main / '.orcexe/decisions.jsonl').open('a') as handle:
                handle.write('{}\n')

        writes = {'decisions': decisions, 'plan': lambda: (self.main / '.orcexe/plan.json').write_text('{}')}
        for name, write in writes.items():
            with self.subTest(name):
                self.saved = self.save()
                self.evaluator([test_task_l1.rubric(passed=True)])
                real = ClaudeCodeAdapter.run

                def run(adapter, req, write=write):
                    write()
                    return real(adapter, req)

                with mock.patch.object(ClaudeCodeAdapter, 'run', new=run):
                    code, result, _ = self.invoke()
                self.assertEqual((code, result['state'], result['reason'], result['land']),
                                 (1, 'rejected', 'main_repository_changed', None), result)
        self.saved = self.save()
        self.evaluator([test_task_l1.rubric(passed=True)])
        real = ClaudeCodeAdapter.run

        def other(adapter, req):
            target = self.main / '.orcexe/runs/task-other'
            target.mkdir(parents=True, exist_ok=True)
            (target / 'contract.lite.json').write_text('{}')
            return real(adapter, req)

        with mock.patch.object(ClaudeCodeAdapter, 'run', new=other):
            code, result, _ = self.invoke()
        self.assert_tampered(code, result)

    def test_corrupt_tail_is_exit_2_with_a_repair_hint_for_task_status_and_plan_run(self):
        self.invoke()
        with ledger.default_path(self.main).open('ab') as handle:
            handle.write(b'{"torn')
        code, result, err = self.invoke()
        self.assertEqual((code, result['reason']), (2, 'ledger_corrupt'), result)
        self.assertIn('orcexe ledger repair', err)
        code, out, err = run_cli(self.env, 'status', '--repo', str(self.main))
        self.assertEqual(code, 2)
        self.assertIn('not_seen: ledger_corrupt', err)
        self.assertIn('orcexe ledger repair', err)
        code, out, err = self.plan_run()
        self.assertEqual((code, json.loads(out)['reason']), (2, 'ledger_corrupt'))
        self.assertIn('orcexe ledger repair', err)
        self.assertEqual(run_cli(self.env, 'ledger', 'repair', '--repo', str(self.main))[0], 0)
        self.assertEqual(run_cli(self.env, 'status', '--repo', str(self.main))[0], 0)

    def plan_run(self, after=None):
        from orcexe.adapters.codex import CodexAdapter

        def factory():
            adapter = CodexAdapter(env=self.env, kill_grace_s=.1)
            real = adapter.run

            def run(request):
                self.requests.append(request)
                result = real(request)
                if after:
                    after(request)
                return result
            adapter.run = run
            return adapter
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(task, 'CodexAdapter', side_effect=factory), \
                redirect_stdout(out), redirect_stderr(err):
            code = cli.main(['plan', '--run', self.saved['plan_id'], '--repo', str(self.main), '--json'])
        return code, out.getvalue(), err.getvalue()


if __name__ == '__main__':
    unittest.main()
