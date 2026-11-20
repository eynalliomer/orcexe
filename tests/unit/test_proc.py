"""Exercise headless streaming, bounded buffers, and process tree ownership."""

from dataclasses import FrozenInstanceError
import errno
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import tracemalloc
import unittest
from unittest import mock

from tests.unit._timing import run_when_ready, scaled, wait_for


SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "orcexe" / "src"
sys.path.insert(0, str(SOURCE))

from orcexe import proc
from orcexe.proc import run


class ProcTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.threads_before = threading.active_count()

    def tearDown(self):
        self.assertEqual(threading.active_count(), self.threads_before,
                         "run must join all its reader threads")

    def child(self, source):
        path = self.directory / "child.py"
        path.write_text(textwrap.dedent(source), encoding="utf-8")
        return [sys.executable, "-u", str(path)]

    def run_child(self, source, ready=False, start=None, **overrides):
        options = dict(cwd=str(self.directory), env={}, timeout_s=scaled(4),
                       stall_s=scaled(2), kill_grace_s=scaled(0.15))
        options.update(overrides)
        argv = self.child(source)
        if ready:
            marker = self.directory / "ready"
            marker.unlink(missing_ok=True)
            return run_when_ready(run, argv, marker.exists, self.addCleanup, start=start, **options)
        return run(argv, **options)

    def test_stdin_is_eof_and_result_is_immutable(self):
        result = self.run_child("from pathlib import Path; import sys; "
                                "assert sys.stdin.read() == ''; Path('ready').touch()", ready=True)
        self.assertEqual((result.status, result.returncode), ("exited", 0))
        self.assertLess(result.duration_s, scaled(2))
        self.assertFalse(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assertIsNone(result.error)
        with self.assertRaises(FrozenInstanceError):
            result.status = "timeout"

    def test_binary_stdin_and_stdout_preserve_exact_bytes(self):
        data = bytes(range(256)) * 1024 + b'\x00\xff\r\n\n'
        chunks = []
        result = self.run_child('import sys; sys.stdout.buffer.write(sys.stdin.buffer.read())',
                                input_data=data, on_stdout_bytes=chunks.append)
        self.assertEqual((result.status, result.returncode), ('exited', 0))
        self.assertEqual(b''.join(chunks), data)
        self.assertEqual(result.stdout_bytes, len(data))

    def test_binary_callback_failure_cleans_up_child(self):
        def fail(chunk):
            raise ValueError('binary output cap')
        with self.assertRaisesRegex(ValueError, 'binary output cap'):
            self.run_child('import sys, time; print("ready", flush=True); time.sleep(60)',
                           on_stdout_bytes=fail)

    def test_env_required_and_never_inherited(self):
        with self.assertRaises(ValueError):
            self.run_child("pass", env=None)
        with self.assertRaises(TypeError):
            run([sys.executable], cwd=str(self.directory), timeout_s=scaled(1), stall_s=scaled(1))
        lines = []
        with mock.patch.dict(os.environ, {"ORCEXE_TEST_PARENT_ONLY": "parent"}):
            result = self.run_child("""
                import os
                print(os.getenv('ORCEXE_TEST_PARENT_ONLY', 'absent'))
                print(os.getenv('ORCEXE_TEST_EXPLICIT', 'absent'))
            """, env={"ORCEXE_TEST_EXPLICIT": "supplied"}, on_line=lines.append)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["absent", "supplied"])

    def test_both_streams_order_replacement_crlf_and_final_line(self):
        out, err = [], []
        result = self.run_child("""
            import os
            os.write(1, b'first\\r\\n\\ninvalid:\\xff\\nlast')
            os.write(2, b'error\\r\\ninvalid:\\xfe\\nfinal')
        """, on_line=out.append, on_stderr_line=err.append)
        self.assertEqual(out, ["first", "", "invalid:\ufffd", "last"])
        self.assertEqual(err, ["error", "invalid:\ufffd", "final"])
        self.assertEqual(result.stdout_lines, 4)
        self.assertEqual(result.stdout_bytes, len(b"first\r\n\ninvalid:\xff\nlast"))
        self.assertEqual(result.stderr_tail, "error\r\ninvalid:\ufffd\nfinal")
        self.assertEqual(result.truncated_lines, 0)

    def test_callbacks_arrive_before_exit_on_calling_thread(self):
        release = self.directory / "release"
        out, err = [], []
        caller = threading.get_ident()

        def stdout(line):
            self.assertEqual(threading.get_ident(), caller)
            out.append(line)
            if line == "ready":
                release.write_text("continue", encoding="utf-8")

        def stderr(line):
            self.assertEqual(threading.get_ident(), caller)
            err.append(line)
            release.unlink()

        result = self.run_child("""
            from pathlib import Path
            import sys, time
            release = Path('release')
            Path('ready').touch()
            print('ready', flush=True)
            deadline = time.monotonic() + %r
            while not release.exists():
                assert time.monotonic() < deadline, 'child never became ready'
                time.sleep(.01)
            print('stderr-ready', file=sys.stderr, flush=True)
            deadline = time.monotonic() + %r
            while release.exists():
                assert time.monotonic() < deadline, 'child never became ready'
                time.sleep(.01)
            print('done')
        """ % (scaled(15), scaled(15)), ready=True, on_line=stdout, on_stderr_line=stderr)
        self.assertEqual((result.status, result.returncode), ("exited", 0))
        self.assertEqual(out, ["ready", "done"])
        self.assertEqual(err, ["stderr-ready"])

    def test_truncation_utf8_boundaries_discards_and_counts(self):
        out, err = [], []
        result = self.run_child("""
            import os
            os.write(1, 'ab€ignored\\nnext\\n12345\\nlast-long'.encode())
            os.write(2, 'ab€ignored\\nend'.encode())
        """, max_line_bytes=4, on_line=out.append, on_stderr_line=err.append)
        self.assertEqual(out, ["ab", "next", "1234", "last"])
        self.assertEqual(err, ["ab", "end"])
        self.assertEqual(result.stdout_lines, 4)
        self.assertEqual(result.truncated_lines, 4)
        self.assertEqual(result.stdout_bytes, len("ab€ignored\nnext\n12345\nlast-long".encode()))
        self.assertTrue(all(len(line.encode()) <= 4 for line in out + err))

    def test_chunk_boundary_utf8_and_exact_limit(self):
        lines = []
        result = self.run_child("""
            import os, time
            os.write(1, b'ab\\xe2')
            time.sleep(.05)
            os.write(1, b'\\x82\\xac\\n12345\\r\\n')
        """, max_line_bytes=5, on_line=lines.append)
        self.assertEqual(lines, ["ab€", "12345"])
        self.assertEqual(result.truncated_lines, 0)

    def test_zero_limits_and_bare_carriage_return(self):
        out, err = [], []
        result = self.run_child("""
            import os
            os.write(1, b'abc\\n\\nlast')
            os.write(2, b'error')
        """, max_line_bytes=0, stderr_tail_bytes=0,
                                on_line=out.append, on_stderr_line=err.append)
        self.assertEqual(out, ["", "", ""])
        self.assertEqual(err, [""])
        self.assertEqual(result.truncated_lines, 3)
        self.assertEqual(result.stderr_tail, "")
        out.clear()
        self.run_child("import os; os.write(1, b'bare\\r')", on_line=out.append)
        self.assertEqual(out, ["bare\r"])

    def test_stderr_tail_keeps_end_with_utf8_byte_cap(self):
        result = self.run_child("import os; os.write(2, b'prefix-' + '€XYZ'.encode())",
                                stderr_tail_bytes=5)
        self.assertTrue(result.stderr_tail.endswith("XYZ"))
        self.assertLessEqual(len(result.stderr_tail.encode()), 5)
        result = self.run_child("import os; os.write(2, b'prefix\\xffXYZ')",
                                stderr_tail_bytes=4)
        self.assertTrue(result.stderr_tail.endswith("XYZ"))
        self.assertLessEqual(len(result.stderr_tail.encode()), 4)
        result = self.run_child("import os; os.write(2, b'prefix-tail')",
                                stderr_tail_bytes=4)
        self.assertEqual(result.stderr_tail, "tail")

    def test_large_lines_and_totals_have_bounded_memory(self):
        count = 0

        def discard(line):
            nonlocal count
            count += 1

        tracemalloc.start()
        try:
            baseline = tracemalloc.get_traced_memory()[0]
            result = self.run_child("""
                import os
                block = b'x' * (1024 * 1024)
                for _ in range(10):
                    os.write(1, block)
                os.write(1, b'\\n')
                block = b'y' * 1023 + b'\\n'
                for _ in range(10240):
                    os.write(1, block)
                    os.write(2, block)
            """, on_line=discard, on_stderr_line=discard, timeout_s=scaled(8), stall_s=scaled(4))
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual((result.status, result.returncode), ("exited", 0))
        self.assertEqual(result.stdout_bytes, 20 * 1024 * 1024 + 1)
        self.assertEqual(result.stdout_lines, 10241)
        self.assertEqual(result.truncated_lines, 1)
        self.assertEqual(count, 20481)
        self.assertLess(peak - baseline, 50 * 1024 * 1024)
        self.assertEqual(len(result.stderr_tail.encode()), 65536)

    def test_timeout_observed_despite_claimed_success(self):
        lines = []
        result = self.run_child("""
            import time
            from pathlib import Path
            print('status=exited returncode=0', flush=True)
            Path('ready').touch()
            time.sleep(3600)
        """, ready=True, timeout_s=scaled(0.3), stall_s=scaled(2), on_line=lines.append)
        self.assertEqual(result.status, "timeout")
        self.assertGreaterEqual(result.duration_s, scaled(.3))
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assertIsNotNone(result.returncode)
        self.assertLess(result.duration_s, scaled(3))
        self.assertEqual(lines, ["status=exited returncode=0"])

    def test_stall_on_silent_child(self):
        result = self.run_child("from pathlib import Path; import time; Path('ready').touch(); time.sleep(300)",
                                ready=True, timeout_s=scaled(3), stall_s=scaled(0.2))
        self.assertEqual(result.status, "stalled")
        self.assertGreaterEqual(result.duration_s, scaled(.2))
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assertLess(result.duration_s, scaled(2))

    @unittest.skipIf(os.name == "nt", "POSIX SIGTERM handler")
    def test_killed_tree_waits_for_late_pipe_eof(self):
        for reason in ("stalled", "timeout", "cancelled"):
            with self.subTest(reason=reason):
                release_eof = threading.Event()
                stopped = threading.Event()
                notify_at = []
                deferred_polls = []
                cancel = threading.Event()
                real_reader = proc._reader
                real_stop = proc._stop_posix

                def reader(pipe, stream, chunks, stop, activity, failures, eof):
                    class LateEOF:
                        def set(self):
                            if not stopped.wait(scaled(15)):
                                raise AssertionError("tree cleanup never completed")
                            release_eof.wait(max(0.0, notify_at[0] - time.monotonic()))
                            eof.set()

                    real_reader(pipe, stream, chunks, stop, activity, failures, LateEOF())

                def stop(process, grace):
                    outcome = real_stop(process, grace)
                    poll = process.poll

                    def delayed_reap():
                        # Force the scheduler race at the first post-stop poll,
                        # regardless of whether the OS already reaped the child.
                        process.poll = poll
                        deferred_polls.append(True)
                        return None

                    process.poll = delayed_reap
                    # Simulate readers scheduled after the old short window,
                    # measured from completed cleanup rather than child startup.
                    notify_at.append(time.monotonic() + scaled(.12))
                    stopped.set()
                    return outcome

                try:
                    with mock.patch.object(proc, "_DRAIN", scaled(.04)), \
                            mock.patch.object(proc, "_reader", side_effect=reader), \
                            mock.patch.object(proc, "_stop_posix", side_effect=stop):
                        result = self.run_child("""
                            import os, signal, sys, time
                            from pathlib import Path
                            def close_slowly(signum, frame):
                                os.close(1)
                                os.close(2)
                                sys.exit(0)
                            signal.signal(signal.SIGTERM, close_slowly)
                            Path('ready').touch()
                            time.sleep(3600)
                        """, ready=True,
                            start=cancel.set if reason == "cancelled" else None,
                            cancel_event=cancel,
                            timeout_s=scaled(.2 if reason == "timeout" else 5),
                            stall_s=scaled(.2) if reason == "stalled" else None,
                            kill_grace_s=scaled(.3))
                finally:
                    release_eof.set()
                self.assertEqual((result.status, result.returncode), (reason, 0))
                self.assertTrue(result.tree_killed)
                self.assertFalse(result.pipes_held, result)
                self.assertEqual(deferred_polls, [True])
                self.assertLess(result.duration_s, scaled(4))

    def test_either_stream_bytes_reset_stall_without_newlines(self):
        for descriptor in (1, 2):
            with self.subTest(descriptor=descriptor):
                release = self.directory / "release"
                release.unlink(missing_ok=True)
                result = self.run_child("""
                    import os, time
                    from pathlib import Path
                    Path('ready').touch()
                    deadline = time.monotonic() + %r
                    while not Path('release').exists():
                        assert time.monotonic() < deadline, 'child never became ready'
                        time.sleep(.01)
                    for _ in range(6):
                        os.write(%d, b'x')
                        time.sleep(%r)
                """ % (scaled(15), descriptor, scaled(.07)), ready=True,
                    start=release.touch, timeout_s=scaled(3), stall_s=scaled(0.3))
                self.assertEqual((result.status, result.returncode), ("exited", 0))
                self.assertFalse(result.tree_killed)

    def test_cancel_event_is_prompt(self):
        event = threading.Event()
        ready_at = []

        def cancel(line):
            self.assertEqual(line, "ready")
            ready_at.append(time.monotonic())
            event.set()

        result = self.run_child("import time; print('ready', flush=True); time.sleep(3600)",
                                on_line=cancel, cancel_event=event,
                                timeout_s=scaled(15), stall_s=None)
        self.assertTrue(ready_at, "child never became ready")
        self.assertLess(time.monotonic() - ready_at[0], scaled(2))
        self.assertEqual(result.status, "cancelled")
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)

    def run_escaped_holder(self, **overrides):
        """Keep the leader alive through setup; measure only its final drain."""
        sleeper_pid = self.directory / "escaped.pid"
        leader_pid = self.directory / "leader.pid"
        release = self.directory / "release"
        monitor_stop = threading.Event()
        cancelled_at = []
        event = overrides.get("cancel_event")
        lines = []

        def cleanup():
            if sleeper_pid.exists():
                try:
                    os.kill(int(sleeper_pid.read_text()), signal.SIGKILL)
                except (ProcessLookupError, ValueError):
                    pass

        self.addCleanup(cleanup)

        def cancel_after_exit():
            deadline = time.monotonic() + scaled(15)
            while not monitor_stop.wait(.01):
                try:
                    os.kill(int(leader_pid.read_text()), 0)
                except ProcessLookupError:
                    cancelled_at.append(time.monotonic())
                    event.set()
                    return
                if time.monotonic() >= deadline:
                    return

        watchdog = threading.Timer(scaled(3), cleanup)
        monitor = threading.Thread(target=cancel_after_exit) if event else None

        def ready_line(line):
            lines.append(line)
            watchdog.start()
            if monitor is not None:
                monitor.start()
            release.touch()

        try:
            # Keep the production drain window longer than the short remaining
            # timeout at every scale, so ignoring that timeout still fails.
            with mock.patch.object(proc, "_DRAIN", scaled(proc._DRAIN)):
                result = self.run_child("""
                    import os, subprocess, sys, time
                    from pathlib import Path
                    p = subprocess.Popen(
                        [sys.executable, '-c', 'import time; time.sleep(3600)'],
                        start_new_session=True)
                    Path('escaped.pid').write_text(str(p.pid))
                    Path('leader.pid').write_text(str(os.getpid()))
                    Path('ready').touch()
                    print('leader line', flush=True)
                    deadline = time.monotonic() + %r
                    while not Path('release').exists():
                        assert time.monotonic() < deadline, 'child never became ready'
                        time.sleep(.01)
                    sys.exit(7)
                """ % scaled(15), ready=True, stall_s=None,
                    on_line=ready_line, **overrides)
            returned_at = time.monotonic()
            # The runner must not kill a process outside its group.
            os.kill(int(sleeper_pid.read_text()), 0)
        finally:
            monitor_stop.set()
            watchdog.cancel()
            if watchdog.ident is not None:
                watchdog.join()
            if monitor is not None and monitor.ident is not None:
                monitor.join()
            cleanup()
        self.assertEqual((result.status, result.returncode), ("exited", 7))
        self.assertTrue(result.pipes_held)
        self.assertFalse(result.tree_killed)
        self.assertEqual(lines, ["leader line"])
        return result, returned_at, cancelled_at

    @unittest.skipIf(os.name == "nt", "POSIX escaped session and PID cleanup")
    def test_escaped_holder_has_bounded_post_exit_drain(self):
        result, _, _ = self.run_escaped_holder(timeout_s=scaled(2))
        self.assertLess(result.duration_s, scaled(1.5))

    @unittest.skipIf(os.name == "nt", "POSIX escaped session and PID cleanup")
    def test_escaped_holder_drain_is_bounded_without_timeout(self):
        result, _, _ = self.run_escaped_holder(timeout_s=None)
        self.assertLess(result.duration_s, scaled(1.5))

    @unittest.skipIf(os.name == "nt", "POSIX escaped session and PID cleanup")
    def test_escaped_holder_drain_respects_remaining_timeout(self):
        result, _, _ = self.run_escaped_holder(timeout_s=scaled(.25))
        self.assertLess(result.duration_s, scaled(.45))

    @unittest.skipIf(os.name == "nt", "POSIX escaped session and PID cleanup")
    def test_cancel_after_leader_exit_stops_escaped_holder_drain(self):
        event = threading.Event()
        _, returned_at, cancelled_at = self.run_escaped_holder(
            timeout_s=scaled(2), cancel_event=event)
        self.assertTrue(event.is_set())
        self.assertEqual(len(cancelled_at), 1)
        self.assertLess(returned_at - cancelled_at[0], scaled(.3))

    def test_nonzero_exit_and_closed_pipes_are_observed(self):
        result = self.run_child("import os, sys; os.close(1); os.close(2); sys.exit(7)")
        self.assertEqual((result.status, result.returncode), ("exited", 7))
        self.assertFalse(result.tree_killed)
        self.assertEqual(result.stdout_bytes, 0)
        self.assertGreaterEqual(result.duration_s, 0)

    def test_spawn_errors_missing_executable_and_cwd(self):
        for argv, cwd in (([str(self.directory / "missing")], str(self.directory)),
                          ([sys.executable], str(self.directory / "missing"))):
            with self.subTest(argv=argv, cwd=cwd):
                result = run(argv, cwd=cwd, env={}, timeout_s=scaled(1), stall_s=scaled(1))
                self.assertEqual(result.status, "spawn_error")
                self.assertIsNone(result.returncode)
                self.assertTrue(result.error)
                self.assertFalse(result.tree_killed)
                self.assertFalse(result.pipes_held)
                self.assertEqual((result.stdout_bytes, result.stdout_lines), (0, 0))

    def test_callback_errors_cleanup_before_reraise(self):
        for stream in ("on_line", "on_stderr_line"):
            with self.subTest(stream=stream):
                ready_at = []

                def fail(line):
                    ready_at.append(time.monotonic())
                    raise RuntimeError("callback failed")

                with self.assertRaisesRegex(RuntimeError, "callback failed"):
                    self.run_child("""
                        import os, time
                        from pathlib import Path
                        os.write(1, b'line\\n')
                        os.write(2, b'line\\n')
                        Path('ready').touch()
                        time.sleep(3600)
                    """, ready=True, **{stream: fail})
                self.assertLess(time.monotonic() - ready_at[0], scaled(2))

    def test_final_line_callback_error_also_cleans_up(self):
        def fail(line):
            raise LookupError("final line")

        with self.assertRaisesRegex(LookupError, "final line"):
            self.run_child("import os; os.write(1, b'last')", on_line=fail)

    @unittest.skipIf(os.name == "nt", "POSIX process group errors")
    def test_group_stop_handles_missing_and_denied_groups_at_every_signal(self):
        for error in (ProcessLookupError(errno.ESRCH, "gone"),
                      PermissionError(errno.EPERM, "denied")):
            for signals in ([0], [0, signal.SIGTERM],
                            [0, signal.SIGTERM, 0],
                            [0, signal.SIGTERM, signal.SIGKILL]):
                with self.subTest(error=type(error), signals=signals):
                    process = mock.Mock(pid=123)

                    def killpg(pid, sig):
                        self.assertTrue(process.poll.called, "reap before probing")
                        if sig == signals[-1] and kill.call_count == len(signals):
                            raise error

                    grace = scaled(.1) if signals[-1] == 0 and len(signals) > 1 else 0
                    with mock.patch("orcexe.proc.os.killpg", side_effect=killpg) as kill:
                        stopped = proc._stop_posix(process, grace)
                    self.assertEqual(stopped, isinstance(error, ProcessLookupError)
                                     or signals == [0, signal.SIGTERM, 0])
                    self.assertEqual(kill.call_args_list,
                                     [mock.call(123, sig) for sig in signals])
                    process.kill.assert_not_called()
                    process.terminate.assert_not_called()
                    process.wait.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process group errors")
    def test_post_term_denied_probe_reaps_only_within_remaining_grace(self):
        for exits in (True, False):
            with self.subTest(exits=exits):
                process = mock.Mock(pid=123, returncode=None)
                process.poll.return_value = None
                grace = scaled(.1)

                def reap(*, timeout):
                    self.assertGreaterEqual(timeout, 0)
                    self.assertLessEqual(timeout, grace)
                    if not exits:
                        raise subprocess.TimeoutExpired("child", timeout)
                    process.returncode = 7
                    return 7

                process.wait.side_effect = reap
                with mock.patch.object(proc.os, "killpg", side_effect=[
                        None, None, PermissionError(errno.EPERM, "denied")]) as killpg:
                    stopped = proc._stop_posix(process, grace)
                self.assertEqual(stopped, exits)
                self.assertEqual(process.returncode, 7 if exits else None)
                process.wait.assert_called_once()
                self.assertEqual(killpg.call_args_list, [mock.call(123, 0),
                                 mock.call(123, signal.SIGTERM), mock.call(123, 0)])
                process.kill.assert_not_called()
                process.terminate.assert_not_called()

    @unittest.skipIf(os.name == "nt", "POSIX process group errors")
    def test_eperm_is_bounded_during_normal_stop_and_callback_cleanup(self):
        for callback_error in (False, True):
            with self.subTest(callback_error=callback_error):
                marker = self.directory / "ready"
                marker.unlink(missing_ok=True)
                argv = self.child("""
                    import time
                    from pathlib import Path
                    print('ready', flush=True)
                    Path('ready').touch()
                    time.sleep(3600)
                """)
                process = subprocess.Popen(
                    argv, cwd=str(self.directory), env={}, stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                    start_new_session=True)
                original = LookupError("callback failed")
                callback_at = []

                def fail(line):
                    callback_at.append(time.monotonic())
                    raise original

                try:
                    wait_for(marker.exists)
                    with mock.patch("orcexe.proc.subprocess.Popen", return_value=process), \
                            mock.patch("orcexe.proc.os.killpg", side_effect=PermissionError(
                                errno.EPERM, "denied")) as killpg, \
                            mock.patch.object(process, "kill", wraps=process.kill) as kill:
                        before = time.monotonic()
                        options = dict(cwd=str(self.directory), env={}, timeout_s=scaled(15 if callback_error else .1),
                                       stall_s=None, kill_grace_s=scaled(0))
                        if callback_error:
                            with self.assertRaises(LookupError) as caught:
                                run(argv, on_line=fail, **options)
                            self.assertIs(caught.exception, original)
                        else:
                            result = run(argv, **options)
                            self.assertEqual(result.status, "timeout")
                            self.assertTrue(result.tree_killed)
                            self.assertIsNone(result.returncode)
                            self.assertTrue(result.pipes_held)
                        self.assertLess(time.monotonic() - (callback_at[0] if callback_error else before),
                                        scaled(1))
                        self.assertEqual(killpg.call_count, 1)
                        self.assertEqual(killpg.call_args, mock.call(process.pid, 0))
                        kill.assert_not_called()
                finally:
                    # Only the test owns this deliberately unsignalable live child.
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=scaled(5))
                    process.stdout.close()
                    process.stderr.close()

    @unittest.skipIf(os.name == "nt", "POSIX cleanup injection")
    def test_cleanup_failures_preserve_callback_interrupt_and_reader_errors(self):
        for origin in ("callback", "interrupt", "reader"):
            for cleanup in ("stop", "close"):
                with self.subTest(origin=origin, cleanup=cleanup):
                    original = (KeyboardInterrupt("interrupted") if origin == "interrupt"
                                else OSError("reader failed") if origin == "reader"
                                else LookupError("callback failed"))

                    observed_at = []

                    def fail(line):
                        observed_at.append(time.monotonic())
                        raise original

                    def reader(pipe, stream, chunks, stop, activity, failures, eof):
                        observed_at.append(time.monotonic())
                        failures.append(original)

                    real_stop = proc._stop_posix

                    def stop(process, grace):
                        real_stop(process, grace)
                        if cleanup == "stop":
                            raise RuntimeError("stop failed")

                    real_popen = subprocess.Popen

                    def popen(*args, **kwargs):
                        process = real_popen(*args, **kwargs)
                        if cleanup == "close":
                            close = process.stdout.close

                            def failed_close():
                                close()
                                raise RuntimeError("close failed")

                            process.stdout.close = failed_close
                        return process

                    with mock.patch("orcexe.proc._stop_posix", side_effect=stop), \
                            mock.patch("orcexe.proc.subprocess.Popen", side_effect=popen), \
                            mock.patch("orcexe.proc._reader",
                                       side_effect=reader if origin == "reader" else proc._reader):
                        with self.assertRaises(type(original)) as caught:
                            self.run_child("print('line', flush=True)", on_line=fail)
                        self.assertIs(caught.exception, original)
                        self.assertLess(time.monotonic() - observed_at[0], scaled(1))

    def test_negative_limits_rejected(self):
        for name in ("timeout_s", "stall_s", "max_line_bytes",
                     "stderr_tail_bytes", "kill_grace_s"):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    self.run_child("pass", **{name: -1})

    @unittest.skipIf(os.name == "nt", "POSIX session and killpg semantics")
    def test_child_starts_in_own_posix_session(self):
        result = self.run_child("""
            import os
            assert os.getpid() == os.getpgrp() == os.getsid(0)
        """)
        self.assertEqual(result.returncode, 0)

    @unittest.skipIf(os.name == "nt", "SIGTERM/SIGKILL are POSIX signals")
    def test_sigterm_ignoring_child_is_killed_after_grace(self):
        sent = []
        killpg = os.killpg

        def record_signal(pid, sig):
            if sig:
                sent.append((sig, time.monotonic()))
            return killpg(pid, sig)

        with mock.patch.object(proc.os, "killpg", side_effect=record_signal):
            result = self.run_child("""
                import signal, time
                from pathlib import Path
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                Path('ready').touch()
                print('ready', flush=True)
                time.sleep(3600)
            """, ready=True, timeout_s=scaled(0.3), kill_grace_s=scaled(0.15))
        self.assertEqual(result.status, "timeout")
        self.assertEqual(result.returncode, -signal.SIGKILL)
        self.assertEqual([sig for sig, _ in sent], [signal.SIGTERM, signal.SIGKILL])
        self.assertGreaterEqual(sent[1][1] - sent[0][1], scaled(.15))
        self.assertGreaterEqual(result.duration_s, scaled(.3) + scaled(.15))
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assertLess(result.duration_s, scaled(3))

    def tree_child(self, background):
        def cleanup_grandchild():
            path = self.directory / "grandchild.pid"
            if path.exists():
                try:
                    os.kill(int(path.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.addCleanup(cleanup_grandchild)
        grandchild = self.directory / "grandchild.py"
        grandchild.write_text(textwrap.dedent("""
            import os, time
            from pathlib import Path
            Path('grandchild.tmp').write_text(str(os.getpid()))
            Path('grandchild.tmp').replace('grandchild.pid')
            time.sleep(3600)
        """), encoding="utf-8")
        # Reap on TERM when the parent remains alive. This also tests on POSIX
        # hosts whose PID 1 does not reap orphan zombies (kill(pid, 0) sees them).
        return """
            import os, signal, subprocess, sys, time
            from pathlib import Path
            p = subprocess.Popen([sys.executable, '-u', %r], env={})
            if os.name != 'nt':
                def reap(signum, frame):
                    p.wait(timeout=%r)
                    sys.exit(0)
                signal.signal(signal.SIGTERM, reap)
            deadline = time.monotonic() + %r
            while not Path('grandchild.pid').exists():
                assert time.monotonic() < deadline, 'child never became ready'
                time.sleep(.01)
            Path('ready').touch()
            print('ready', flush=True)
            %s
        """ % (str(grandchild), scaled(15), scaled(15),
                   ("deadline = time.monotonic() + %r\n"
                    "            while not Path('release').exists():\n"
                    "                assert time.monotonic() < deadline, 'child never became ready'\n"
                    "                time.sleep(.01)\n"
                    "            sys.exit(7)" % scaled(15)) if background else "time.sleep(3600)")

    def assert_grandchild_gone(self):
        pid = int((self.directory / "grandchild.pid").read_text())
        if os.name == "nt":
            # TASKLIST is only used on Windows; no shell or network involved.
            listing = subprocess.check_output(
                ["tasklist", "/FI", "PID eq {}".format(pid), "/FO", "CSV", "/NH"])
            self.assertNotIn('"{}"'.format(pid), listing.decode(errors="replace"))
        else:
            deadline = time.monotonic() + scaled(1)
            while True:
                try:
                    os.kill(pid, 0)
                except ProcessLookupError:
                    return
                if time.monotonic() >= deadline:
                    self.fail("Grandchild {} is still present".format(pid))
                time.sleep(.02)

    @unittest.skipIf(os.name == "nt", "POSIX grandchild check uses kill(pid, 0)")
    def test_timeout_kills_posix_grandchild(self):
        result = self.run_child(self.tree_child(False), ready=True, timeout_s=scaled(0.4))
        self.assertEqual(result.status, "timeout")
        self.assertGreaterEqual(result.duration_s, scaled(.4))
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assertLess(result.duration_s, scaled(4))
        self.assert_grandchild_gone()

    @unittest.skipIf(os.name == "nt", "POSIX background process group cleanup")
    def test_normal_exit_kills_background_posix_grandchild(self):
        result = self.run_child(self.tree_child(True), ready=True,
                                start=(self.directory / "release").touch)
        self.assertEqual((result.status, result.returncode), ("exited", 7))
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assertLess(result.duration_s, scaled(4))
        self.assert_grandchild_gone()

    @unittest.skipIf(os.name != "nt", "Windows taskkill/tasklist integration")
    def test_timeout_kills_windows_grandchild(self):
        result = self.run_child(self.tree_child(False), ready=True, timeout_s=scaled(0.5))
        self.assertEqual(result.status, "timeout")
        self.assertGreaterEqual(result.duration_s, scaled(.5))
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assertLess(result.duration_s, scaled(5))
        self.assert_grandchild_gone()

    @unittest.skipIf(os.name != "nt", "Windows Job Object background cleanup")
    def test_normal_exit_kills_background_windows_grandchild(self):
        result = self.run_child(self.tree_child(True), ready=True,
                                start=(self.directory / "release").touch)
        self.assertEqual((result.status, result.returncode), ("exited", 7))
        self.assertTrue(result.tree_killed)
        self.assertFalse(result.pipes_held)
        self.assert_grandchild_gone()
