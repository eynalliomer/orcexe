"""Bounded, headless child execution with observed status and tree cleanup."""

import codecs
from dataclasses import dataclass
import os
import queue
import select
import signal
import subprocess
import threading
import time
from typing import Optional


@dataclass(frozen=True)
class ProcResult:
    status: str
    returncode: Optional[int]
    duration_s: float
    stdout_lines: int
    stdout_bytes: int
    truncated_lines: int
    stderr_tail: str
    tree_killed: bool
    error: Optional[str]
    pipes_held: bool = False


_CHUNK = 16384
_POLL = 0.02
_DRAIN = 0.5


class _Lines:
    def __init__(self, limit, callback):
        self.limit = limit
        self.callback = callback
        self.pending = bytearray()
        self.truncated = False
        self.lines = 0
        self.truncated_lines = 0

    def _emit(self, newline):
        data = self.pending
        if newline and not self.truncated and data.endswith(b"\r"):
            del data[-1:]
        if len(data) > self.limit:
            del data[self.limit:]
            self.truncated = True
        if self.truncated:
            # An incomplete UTF-8 character at the cap is not part of the line.
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            line = decoder.decode(data, final=False)
        else:
            line = data.decode("utf-8", errors="replace")
        self.lines += 1
        self.truncated_lines += int(self.truncated)
        self.pending = bytearray()
        self.truncated = False
        if self.callback is not None:
            self.callback(line)

    def feed(self, chunk):
        start = 0
        while start < len(chunk):
            end = chunk.find(b"\n", start)
            stop = len(chunk) if end < 0 else end
            # One byte of lookahead lets CRLF at the cap remain untruncated.
            room = self.limit + 1 - len(self.pending)
            self.pending.extend(chunk[start:min(stop, start + room)])
            if stop - start > room:
                self.truncated = True
            if end < 0:
                break
            self._emit(newline=True)
            start = end + 1

    def finish(self):
        if self.pending or self.truncated:
            self._emit(newline=False)


class _WindowsJob:
    """Track descendants even if their parent exits before taskkill runs.

    Spawn suspended, assign to the job, then resume the initial thread. This
    closes the usual race between Popen and assigning a running child to a job.
    All handles are private to this run and closed before returning.
    """

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        k = self.kernel
        k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k.CreateJobObjectW.restype = wintypes.HANDLE
        k.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                               ctypes.c_void_p, wintypes.DWORD,
                                               ctypes.c_void_p]
        k.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k.CloseHandle.argtypes = [wintypes.HANDLE]
        k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        k.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        k.OpenThread.restype = wintypes.HANDLE
        k.ResumeThread.argtypes = [wintypes.HANDLE]
        k.ResumeThread.restype = wintypes.DWORD
        k.GetSystemDirectoryW.argtypes = [wintypes.LPWSTR, wintypes.UINT]
        self.handle = k.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def attach_and_resume(self, process):
        c, w, k = self.ctypes, self.wintypes, self.kernel
        if not k.AssignProcessToJobObject(self.handle, int(process._handle)):
            raise c.WinError(c.get_last_error())

        class ThreadEntry(c.Structure):
            _fields_ = [("size", w.DWORD), ("usage", w.DWORD),
                        ("tid", w.DWORD), ("pid", w.DWORD),
                        ("base_priority", w.LONG), ("delta_priority", w.LONG),
                        ("flags", w.DWORD)]

        k.Thread32First.argtypes = [w.HANDLE, c.POINTER(ThreadEntry)]
        k.Thread32Next.argtypes = [w.HANDLE, c.POINTER(ThreadEntry)]
        snapshot = k.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
        if snapshot == c.c_void_p(-1).value:
            raise c.WinError(c.get_last_error())
        try:
            entry = ThreadEntry()
            entry.size = c.sizeof(entry)
            more = k.Thread32First(snapshot, c.byref(entry))
            while more:
                if entry.pid == process.pid:
                    thread = k.OpenThread(2, False, entry.tid)  # THREAD_SUSPEND_RESUME
                    if not thread:
                        raise c.WinError(c.get_last_error())
                    try:
                        if k.ResumeThread(thread) == 0xFFFFFFFF:
                            raise c.WinError(c.get_last_error())
                    finally:
                        k.CloseHandle(thread)
                    return
                more = k.Thread32Next(snapshot, c.byref(entry))
            raise OSError("Child's initial thread was not found")
        finally:
            k.CloseHandle(snapshot)

    def alive(self):
        c, w = self.ctypes, self.wintypes

        class Accounting(c.Structure):
            _fields_ = [("times", c.c_longlong * 4), ("faults", w.DWORD),
                        ("total", w.DWORD), ("active", w.DWORD),
                        ("terminated", w.DWORD)]

        info = Accounting()
        if not self.kernel.QueryInformationJobObject(
                self.handle, 1, c.byref(info), c.sizeof(info), None):
            raise c.WinError(c.get_last_error())
        return info.active != 0

    def stop(self, process, env, grace):
        c = self.ctypes
        directory = c.create_unicode_buffer(32768)
        length = self.kernel.GetSystemDirectoryW(directory, len(directory))
        try:
            if not length or length >= len(directory):
                raise OSError("Cannot locate taskkill")
            subprocess.run(
                [os.path.join(directory.value, "taskkill.exe"),
                 "/T", "/F", "/PID", str(process.pid)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=env, timeout=max(1.0, grace),
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass  # The job remains an authoritative fallback for every member.
        finally:
            # taskkill cannot find a tree whose root has already exited.
            if not self.kernel.TerminateJobObject(self.handle, 1):
                raise c.WinError(c.get_last_error())

    def close(self):
        self.kernel.CloseHandle(self.handle)


def _group_alive(pid):
    try:
        os.killpg(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        # macOS may report EPERM for a group containing only a zombie leader.
        # In either case there is nothing left that we may signal.
        return False


def _stop_posix(process, grace):
    """Return whether the tree was stopped; denied stops return False."""
    process.poll()  # Reap before deciding whether the group needs signalling.
    try:
        # Unlike _group_alive, distinguish a vanished group from a denied stop.
        os.killpg(process.pid, 0)
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()  # Reap the leader so it does not keep the group alive.
        if not _group_alive(process.pid):
            # macOS can report EPERM for a zombie-only group before poll sees
            # the leader's exit. Reap within the existing grace to distinguish
            # this from a still-live, unsignalable leader.
            if process.returncode is None:
                try:
                    process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    return False
            return True
        time.sleep(min(_POLL, max(0.0, deadline - time.monotonic())))
    process.poll()
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    # Only wait after a successful kill. An unsignalable live leader must not
    # turn a timeout, cancellation or exception into an unbounded wait.
    process.wait()
    return True


def _cleanup_preserving(error, action, *args):
    """Attempt each cleanup without replacing an exception already in flight."""
    try:
        action(*args)
    except BaseException:
        if error is None:
            raise


def _reader(pipe, stream, chunks, stop, activity, failures, eof):
    """Read only ready bytes, so even an escaped pipe holder cannot pin a thread."""
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            import msvcrt

            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            peek = kernel.PeekNamedPipe
            peek.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                             ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
                             ctypes.c_void_p]
            handle = msvcrt.get_osfhandle(pipe.fileno())
        while not stop.is_set():
            size = _CHUNK
            if os.name == "nt":
                available = wintypes.DWORD()
                if not peek(handle, None, 0, None, ctypes.byref(available), None):
                    if ctypes.get_last_error() == 109:  # ERROR_BROKEN_PIPE
                        eof.set()
                        break
                    raise ctypes.WinError(ctypes.get_last_error())
                if not available.value:
                    stop.wait(_POLL)
                    continue
                size = min(size, available.value)
            elif not select.select([pipe], [], [], _POLL)[0]:
                continue
            data = os.read(pipe.fileno(), size)
            if not data:
                eof.set()
                break
            activity[stream] = time.monotonic()
            while not stop.is_set():
                try:
                    chunks.put((stream, data), timeout=_POLL)
                    break
                except queue.Full:
                    pass
    except BaseException as error:
        failures.append(error)
    finally:
        # EOF notifications use the same bounded queue and preserve byte order.
        while not stop.is_set():
            try:
                chunks.put((stream, None), timeout=_POLL)
                break
            except queue.Full:
                pass


def run(argv, *, cwd, env, timeout_s, stall_s, on_line=None, on_stderr_line=None,
        cancel_event=None, max_line_bytes=1048576, stderr_tail_bytes=65536,
        kill_grace_s=5.0) -> ProcResult:
    """Stream a child with bounded buffers; callbacks execute on this thread.

    Byte counts include delimiters and discarded stdout. Truncated line counts
    cover both streams. A truncated line is delivered once at newline or EOF.
    Timeout/stall limits may be None to disable them; cancellation is checked
    at most every 20 ms while callbacks are not running. Callback exceptions
    propagate after tree cleanup is attempted and both reader threads joined.
    After successfully stopping the tree, allow max(kill_grace_s, 2.0) seconds
    for pipe EOF, independently of when the leader is reaped.
    A timeout-triggered stop gets this cleanup window after its timeout; other
    stops still respect the remaining overall timeout. If the leader exited
    with an empty group/job, drain for at most 0.5 seconds, ending sooner on
    timeout, stall or cancellation. Drain limits keep the observed status and
    returncode; pipes_held reports pipes without EOF when draining stops.
    Processes outside the group/job are never killed.
    After a successful TERM, an unavailable group probe may use the remaining
    kill grace to reap the leader. Denied signals leave live leaders alone,
    with nonblocking reaping and the short 0.5-second pipe drain window.
    """
    if env is None:
        raise ValueError("env must be supplied explicitly")
    if max_line_bytes < 0 or stderr_tail_bytes < 0 or kill_grace_s < 0:
        raise ValueError("buffer limits and kill_grace_s must be nonnegative")
    for limit in (timeout_s, stall_s):
        if limit is not None and limit < 0:
            raise ValueError("time limits must be nonnegative or None")

    started = time.monotonic()
    process = None
    job = None
    try:
        options = {"start_new_session": True}
        if os.name == "nt":
            job = _WindowsJob()
            options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 4}
        process = subprocess.Popen(
            argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0, **options
        )
        if job is not None:
            job.attach_and_resume(process)
    except OSError as error:
        if process is not None:
            _cleanup_preserving(error, process.kill)
            _cleanup_preserving(error, process.wait)
            _cleanup_preserving(error, process.stdout.close)
            _cleanup_preserving(error, process.stderr.close)
        if job is not None:
            _cleanup_preserving(error, job.close)
        return ProcResult("spawn_error", None, time.monotonic() - started,
                          0, 0, 0, "", False, str(error))

    chunks = queue.Queue(maxsize=16)
    stop = threading.Event()
    activity = [started, started]
    failures = []
    readers = []
    lines = [_Lines(max_line_bytes, on_line),
             _Lines(max_line_bytes, on_stderr_line)]
    tail = bytearray()
    stdout_bytes = 0
    tree_killed = False
    tree_stopped = False
    status = "exited"
    finished = set()
    leader_exited = False
    drain_deadline = None
    pipe_eof = [threading.Event(), threading.Event()]
    pipes_held = False
    run_error = None

    def stop_tree():
        nonlocal tree_killed, tree_stopped
        tree_killed = True
        if job is None:
            tree_stopped = _stop_posix(process, kill_grace_s)
        else:
            job.stop(process, env, kill_grace_s)
            process.wait()
            tree_stopped = True

    def consume(stream, data):
        nonlocal stdout_bytes
        if data is None:
            finished.add(stream)
            lines[stream].finish()
        else:
            if stream == 0:
                stdout_bytes += len(data)
            elif stderr_tail_bytes:
                tail.extend(data)
                if len(tail) > stderr_tail_bytes:
                    del tail[:-stderr_tail_bytes]
            lines[stream].feed(data)

    def pipe_deadline():
        if tree_killed:
            process.poll()  # Best-effort reaping, independent of window choice.
        # A denied stop can leave the leader alive; retain its short, bounded
        # return path rather than treating it as a successfully stopped tree.
        window = (max(kill_grace_s, 2.0)
                  if tree_stopped else _DRAIN)
        deadline = time.monotonic() + window
        if timeout_s is not None and status != "timeout":
            deadline = min(deadline, started + timeout_s)
        return deadline

    def drain_after_error():
        # A failed reader cannot report EOF, and an unsignalable live leader
        # must retain the existing prompt exception cleanup behaviour.
        if failures or process.poll() is None:
            return
        deadline = pipe_deadline()
        while not all(eof.is_set() for eof in pipe_eof) and time.monotonic() < deadline:
            try:
                # Free bounded queue space without invoking a failed callback.
                chunks.get(timeout=_POLL)
            except queue.Empty:
                pass

    try:
        for stream, pipe in enumerate((process.stdout, process.stderr)):
            thread = threading.Thread(
                target=_reader,
                args=(pipe, stream, chunks, stop, activity, failures, pipe_eof[stream]),
                name="orcexe-pipe-{}".format(stream),
            )
            thread.start()
            readers.append(thread)
        while True:
            now = time.monotonic()
            if not leader_exited:
                if process.poll() is not None:
                    leader_exited = True
                    alive = job.alive() if job is not None else _group_alive(process.pid)
                    if alive:
                        stop_tree()
                elif cancel_event is not None and cancel_event.is_set():
                    status = "cancelled"
                elif timeout_s is not None and now - started >= timeout_s:
                    status = "timeout"
                elif stall_s is not None and now - max(activity) >= stall_s:
                    status = "stalled"
                if status != "exited":
                    stop_tree()
                    leader_exited = True
                if leader_exited:
                    drain_deadline = pipe_deadline()
            if failures:
                raise failures[0]
            if leader_exited and len(finished) == 2:
                break
            if leader_exited:
                now = time.monotonic()
                if (now >= drain_deadline
                        or (not tree_killed and (
                            (stall_s is not None and now - max(activity) >= stall_s)
                            or (cancel_event is not None and cancel_event.is_set())))):
                    pipes_held = not all(eof.is_set() for eof in pipe_eof)
                    break
            try:
                stream, data = chunks.get(timeout=_POLL)
            except queue.Empty:
                continue
            consume(stream, data)
    except BaseException as error:
        run_error = error
        _cleanup_preserving(error, stop_tree)
        _cleanup_preserving(error, drain_after_error)
        raise
    finally:
        _cleanup_preserving(run_error, stop.set)
        for thread in readers:
            _cleanup_preserving(run_error, thread.join)
        _cleanup_preserving(run_error, process.stdout.close)
        _cleanup_preserving(run_error, process.stderr.close)
        _cleanup_preserving(run_error, process.poll)
        if job is not None:
            _cleanup_preserving(run_error, job.close)

    # Readers are joined: this queue is now finite (at most 16 chunks). Deliver
    # already buffered output even when cancellation ended the pipe drain.
    while True:
        try:
            stream, data = chunks.get_nowait()
        except queue.Empty:
            break
        consume(stream, data)
    for stream, parser in enumerate(lines):
        if stream not in finished:
            parser.finish()

    # Replacement characters may be larger than their source bytes. Re-encode
    # and take a UTF-8-aligned suffix so the public string respects the byte cap.
    stderr_tail = tail.decode("utf-8", errors="replace").encode("utf-8")
    stderr_tail = stderr_tail[-stderr_tail_bytes:] if stderr_tail_bytes else b""
    return ProcResult(
        status, process.returncode, time.monotonic() - started, lines[0].lines,
        stdout_bytes, sum(parser.truncated_lines for parser in lines),
        stderr_tail.decode("utf-8", errors="ignore"), tree_killed, None, pipes_held,
    )
