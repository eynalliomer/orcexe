"""Model-free Codex JSON-RPC, with bounded I/O and session ownership.

Unlike exec (whose stdin is DEVNULL), app-server requires writing protocol
requests to stdin. This is the sole executor protocol that owns a stdin pipe.
"""

import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from orcexe import proc


class AppServerError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


class AppServer:
    MAX_LINE = 1024 * 1024
    TAIL_BYTES = 65536

    def __init__(self, binary, env, timeout_s=10, *, read_timeout_s=None,
                 kill_grace_s=.15):
        if env is None:
            raise ValueError("explicit env required")
        for value in (timeout_s, read_timeout_s if read_timeout_s is not None else timeout_s):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError("deadlines must be finite and positive")
        if not math.isfinite(kill_grace_s) or kill_grace_s < 0:
            raise ValueError("invalid kill grace")
        self.binary, self.env = os.fspath(binary), dict(env)
        self.timeout_s = timeout_s
        self.read_timeout_s = read_timeout_s if read_timeout_s is not None else timeout_s
        self.kill_grace_s = kill_grace_s
        self.process = None
        self._job = None
        self._threads = []
        self._stop = threading.Event()
        self._chunks = queue.Queue(maxsize=16)
        self._writes = queue.Queue(maxsize=1)
        self._failures = []
        self._pending = bytearray()
        self._tail = bytearray()
        self._next_id = 0
        # Counts discarded protocol noise without retaining payload/account data.
        self.ignored_messages = 0
        self._deadline = None
        self._closed = False

    @property
    def stderr_tail(self):
        return self._tail.decode("utf-8", "replace")

    def __enter__(self):
        if self._closed or self.process is not None:
            raise AppServerError(None, "session cannot be reused")
        self._deadline = time.monotonic() + self.timeout_s
        try:
            options = {"start_new_session": True}
            if os.name == "nt":
                self._job = proc._WindowsJob()
                options = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 4}
            self.process = subprocess.Popen(
                [self.binary, "app-server"], env=self.env,
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, bufsize=0, **options)
            if self._job is not None:
                self._job.attach_and_resume(self.process)
            # Reuse the runner's interruptible, bounded pipe readers, including
            # PeekNamedPipe on Windows; a detached pipe holder cannot pin them.
            for stream, pipe in enumerate((self.process.stdout, self.process.stderr)):
                self._threads.append(threading.Thread(
                    target=proc._reader, args=(pipe, stream, self._chunks, self._stop,
                    [time.monotonic()] * 2, self._failures, threading.Event()),
                    name="orcexe-appserver-reader"))
            self._threads.append(threading.Thread(target=self._writer,
                                                  name="orcexe-appserver-writer"))
            for thread in self._threads:
                thread.start()
            version_file = Path(__file__).resolve().parents[3] / ".claude-plugin" / "plugin.json"
            version = json.loads(version_file.read_text(encoding="utf-8"))["version"]
            self.call("initialize", {"clientInfo": {"name": "orcexe", "version": version}})
            self._send({"jsonrpc": "2.0", "method": "initialized"})
            return self
        except OSError:
            self.close()
            raise AppServerError(None, "spawn failed") from None
        except BaseException:
            self.close()
            raise

    def _remaining(self, deadline=None):
        remaining = min(self._deadline, deadline or self._deadline) - time.monotonic()
        if remaining <= 0:
            raise AppServerError(None, "deadline exceeded")
        return remaining

    def _writer(self):
        while not self._stop.is_set():
            try:
                data, done, errors = self._writes.get(timeout=.02)
            except queue.Empty:
                continue
            try:
                view = memoryview(data)
                while view and not self._stop.is_set():
                    count = self.process.stdin.write(view)
                    if not count:
                        raise OSError("stdin closed")
                    view = view[count:]
            except (OSError, ValueError):
                errors.append(True)
            finally:
                done.set()

    def _send(self, message):
        data = (json.dumps(message, allow_nan=False) + "\n").encode("utf-8")
        if len(data) > self.MAX_LINE:
            raise AppServerError(None, "request too long")
        done, errors = threading.Event(), []
        self._writes.put((data, done, errors), timeout=self._remaining())
        if not done.wait(self._remaining()):
            raise AppServerError(None, "deadline exceeded")
        if errors:
            raise AppServerError(None, "stdin closed")

    def _line(self):
        deadline = time.monotonic() + self.read_timeout_s
        while True:
            self._remaining(deadline)
            end = self._pending.find(b"\n")
            if end > self.MAX_LINE or end < 0 and len(self._pending) > self.MAX_LINE:
                raise AppServerError(None, "line too long")
            if end >= 0:
                line = bytes(self._pending[:end])
                del self._pending[:end + 1]
                return line
            if self._failures:
                raise AppServerError(None, "pipe read failed")
            try:
                stream, data = self._chunks.get(timeout=self._remaining(deadline))
            except queue.Empty:
                raise AppServerError(None, "deadline exceeded") from None
            if stream == 1:
                if data:
                    self._tail.extend(data)
                    del self._tail[:-self.TAIL_BYTES]
            elif data is None:
                raise AppServerError(None, "server exited")
            else:
                self._pending.extend(data)

    def call(self, method, params=None):
        if self.process is None or self._closed:
            raise AppServerError(None, "session is not open")
        try:
            request_id = self._next_id
            self._next_id += 1
            self._send({"jsonrpc": "2.0", "id": request_id, "method": method,
                        "params": {} if params is None else params})
            while True:
                try:
                    response = json.loads(self._line(), parse_constant=lambda _: None)
                except (ValueError, UnicodeError, RecursionError):
                    raise AppServerError(None, "malformed JSON") from None
                if isinstance(response, dict) and "method" in response and "id" not in response:
                    continue  # Notification contents and envelope are unrestricted.
                if (not isinstance(response, dict) or type(response.get("id")) is not int
                        or response["id"] != request_id):
                    self.ignored_messages += 1
                    continue
                if "error" in response:
                    error = response["error"]
                    if not isinstance(error, dict) or not isinstance(error.get("message"), str):
                        raise AppServerError(None, "malformed error")
                    raise AppServerError(error.get("code"), error["message"])
                if "result" not in response:
                    raise AppServerError(None, "missing result")
                return response["result"]
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        try:
            if self.process is not None:
                try:
                    self.process.stdin.close()
                finally:
                    if self._job is None:
                        proc._stop_posix(self.process, self.kill_grace_s)
                    else:
                        # The runner's Job handles descendants whose leader
                        # exited, with taskkill /T /F and a Job fallback.
                        self._job.stop(self.process, self.env, self.kill_grace_s)
                    self.process.wait()
        finally:
            for thread in self._threads:
                if thread.ident is not None:
                    thread.join()
            while True:
                try:
                    stream, data = self._chunks.get_nowait()
                except queue.Empty:
                    break
                if stream == 1 and data:
                    self._tail.extend(data)
                    del self._tail[:-self.TAIL_BYTES]
            self._pending.clear()
            if self.process is not None:
                self.process.stdout.close()
                self.process.stderr.close()
            if self._job is not None:
                self._job.close()

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
