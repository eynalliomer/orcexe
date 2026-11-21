"""Exercise real protocol pipes and tree ownership, never a real AI CLI."""

import ast
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from tests.unit._timing import scaled, wait_for

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe import proc
from orcexe.adapters.codex_appserver import AppServer, AppServerError

POPEN = subprocess.Popen


SERVER = r'''
import json, os, signal, subprocess, sys, threading
from pathlib import Path
mode = os.environ["TEST_MODE"]
def emit(value):
    print(json.dumps(value), flush=True)
for line in sys.stdin:
    request = json.loads(line)
    with open(os.environ["TEST_LOG"], "a") as log:
        log.write(json.dumps(request) + "\n")
    if request["method"] == "initialized":
        if mode == "no-read":
            threading.Event().wait()
        continue
    if request["method"] == "initialize":
        if mode in ("tree", "stall"):
            if mode == "tree":
                if os.name != "nt":
                    signal.signal(signal.SIGTERM, signal.SIG_IGN)
                child = subprocess.Popen([sys.executable, "-c",
                    "import signal,threading,os; "
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN) if os.name != 'nt' else None; "
                    "threading.Event().wait()"], stdin=subprocess.DEVNULL)
                Path(os.environ["TEST_CHILD"]).write_text(str(child.pid))
            Path(os.environ["TEST_READY"]).touch()
        emit({"method":"remoteControl/status/changed", "params":{}, "emittedAtMs":1800000000000})
        emit({"id":request["id"], "result":{}})
        continue
    if mode == "malformed":
        print("{bad json", flush=True)
    elif mode == "long":
        print("x" * (1024 * 1024 + 1), flush=True)
    elif mode == "partial":
        sys.stdout.write("x" * (1024 * 1024 + 1)); sys.stdout.flush()
        threading.Event().wait()
    elif mode == "error":
        emit({"id":request["id"], "error":{"code":-32601,"message":"unknown method"}})
    elif mode == "missing-result":
        emit({"id":request["id"]})
    elif mode == "exit":
        sys.exit(0)
    elif mode in ("stall", "tree"):
        threading.Event().wait()
    else:
        for index in range(50 if mode == "storm" else 1):
            emit({"method":"account/updated", "params":[None, index], "emittedAtMs":1800000000000 + index})
        for value in (None, [], "unexpected", 42, {}, {"params":{}},
                      {"id":True, "result":"bool is not our id"},
                      {"id":str(request["id"]), "result":"string is not our id"},
                      {"id":9999, "result":"wrong"}, {"id":9998}):
            emit(value)
        emit({"method":None})  # Notification contents need no validation.
        sys.stderr.write("z" * 200000); sys.stderr.flush()
        response = {"id":request["id"], "result":{"method":request["method"]}}
        if mode == "jsonrpc":
            response["jsonrpc"] = "2.0"
        emit(response)
'''


class AppServerTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orcexe-appserver-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.log = self.directory / "rpc.jsonl"
        self.ready = self.directory / "ready"
        self.child = self.directory / "child"
        self.env = {key: os.environ[key] for key in ("SystemRoot", "WINDIR", "PATH") if key in os.environ}
        self.env.update(HOME=str(self.directory), CODEX_HOME=str(self.directory / "codex"),
                        TEST_LOG=str(self.log), TEST_READY=str(self.ready), TEST_CHILD=str(self.child))
        self.thread_count = threading.active_count()

    def server(self, mode="normal", **options):
        environment = dict(self.env, TEST_MODE=mode)

        def launch(argv, **kwargs):
            if Path(argv[0]).name.lower() == "taskkill.exe":
                return POPEN(argv, **kwargs)
            self.assertEqual(argv, ["fixture-codex", "app-server"])
            self.assertEqual(kwargs["env"], environment)
            self.assertIs(kwargs["stdin"], subprocess.PIPE)
            return POPEN([sys.executable, "-B", "-u", "-c", SERVER], **kwargs)

        patch = mock.patch("orcexe.adapters.codex_appserver.subprocess.Popen", side_effect=launch)
        patch.start()
        self.addCleanup(patch.stop)
        session = AppServer("fixture-codex", environment, timeout_s=scaled(8),
                            kill_grace_s=scaled(.1), **options)
        self.addCleanup(session.close)
        return session

    def assert_closed(self, session):
        self.assertIsNotNone(session.process.poll())
        self.assertTrue(session.process.stdin.closed)
        self.assertTrue(session.process.stdout.closed)
        self.assertTrue(session.process.stderr.closed)
        self.assertTrue(all(not thread.is_alive() for thread in session._threads))
        self.assertEqual(threading.active_count(), self.thread_count)

    def test_handshake_ids_notifications_stderr_and_stdin_closed(self):
        session = self.server()
        with session as s:
            self.assertEqual(s.call("account/read"), {"method": "account/read"})
            self.assertEqual(s.call("model/list"), {"method": "model/list"})
            self.assertEqual(s.ignored_messages, 20)
            self.assertLessEqual(len(s.stderr_tail.encode()), s.TAIL_BYTES)
            self.assertEqual(s.stderr_tail, "z" * s.TAIL_BYTES)
        requests = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertEqual([r["method"] for r in requests],
                         ["initialize", "initialized", "account/read", "model/list"])
        self.assertEqual([r["id"] for r in requests if "id" in r], [0, 1, 2])
        self.assertEqual(requests[0]["params"]["clientInfo"], {"name": "orcexe", "version": json.loads((Path(__file__).resolve().parents[2] / "plugins" / "orcexe" / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["version"]})
        self.assert_closed(session)
        session.close()

    def test_optional_jsonrpc_member(self):
        session = self.server("jsonrpc")
        with session:
            self.assertEqual(session.call("model/list"), {"method": "model/list"})
        self.assert_closed(session)

    def test_fifty_notifications_before_response_within_scaled_bound(self):
        session = self.server("storm")
        with session:
            started = time.monotonic()
            self.assertEqual(session.call("model/list"), {"method": "model/list"})
            self.assertLess(time.monotonic() - started, scaled(2))
            self.assertEqual(session.ignored_messages, 10)
        self.assert_closed(session)

    def test_rpc_error_closes_immediately_and_preserves_code_message(self):
        session = self.server("error")
        with session:
            with self.assertRaises(AppServerError) as caught:
                session.call("bad/method")
            self.assertEqual((caught.exception.code, caught.exception.message), (-32601, "unknown method"))
            self.assert_closed(session)

    def test_bad_lines_caps_and_early_exit(self):
        for mode, reason in (("malformed", "malformed JSON"), ("long", "line too long"),
                             ("partial", "line too long"), ("exit", "server exited"),
                             ("missing-result", "missing result")):
            with self.subTest(mode=mode):
                session = self.server(mode)
                with session:
                    with self.assertRaisesRegex(AppServerError, reason):
                        session.call("test")
                self.assert_closed(session)

    def test_overall_deadline_and_tree_cleanup_after_ready(self):
        session = self.server("tree")
        with session:
            self.assertTrue(self.ready.exists())
            # Set the short deadline after initialization, then wait for a
            # readiness marker before asserting the tree cleanup outcome.
            session._deadline = time.monotonic() + scaled(.8)
            started = time.monotonic()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pending = pool.submit(session.call, "test")
                wait_for(self.ready.exists)
                with self.assertRaisesRegex(AppServerError, "deadline exceeded"):
                    pending.result(timeout=scaled(4))
            self.assertGreaterEqual(time.monotonic() - started, scaled(.8))
            self.assertLess(time.monotonic() - started, scaled(4))
        self.assert_closed(session)
        self.assertTrue(self.child.exists())
        if os.name != "nt":
            wait_for(lambda: not proc._group_alive(session.process.pid), seconds=5)

    def test_per_read_deadline_does_not_wait_for_session_deadline(self):
        session = self.server("stall")
        with session:
            session.read_timeout_s = scaled(.8)
            started = time.monotonic()
            with self.assertRaisesRegex(AppServerError, "deadline exceeded"):
                session.call("test")
            self.assertTrue(self.ready.exists())
            self.assertLess(time.monotonic() - started, scaled(4))
        self.assert_closed(session)

    def test_blocked_stdin_writer_obeys_overall_deadline(self):
        session = self.server("no-read")
        with session:
            session._deadline = time.monotonic() + scaled(.8)
            started = time.monotonic()
            with self.assertRaisesRegex(AppServerError, "deadline exceeded"):
                session.call("test", {"payload": "x" * (256 * 1024)})
            self.assertLess(time.monotonic() - started, scaled(4))
        self.assert_closed(session)

    def test_spawn_failure_and_python39_syntax(self):
        with self.assertRaises(AppServerError):
            with AppServer(str(self.directory / "absent"), self.env, scaled(2)):
                self.fail("unexpected spawn")
        self.assertEqual(threading.active_count(), self.thread_count)
        for name in ("codex.py", "codex_appserver.py"):
            path = ROOT / "plugins" / "orcexe" / "src" / "orcexe" / "adapters" / name
            ast.parse(path.read_text(), feature_version=(3, 9))
