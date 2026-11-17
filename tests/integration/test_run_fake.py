"""Manual executor runs use shipped fakes, private homes and real linked Git trees."""

from contextlib import redirect_stdout
from concurrent.futures import ThreadPoolExecutor
import io
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
from unittest import mock

from tests.unit._timing import scaled, wait_for
from tests.unit.test_gitutil import GitFixture, ROOT
from orcexe import cli, fsio, redact, run, trust
from orcexe.adapters import validate_run_result


FAKES = ROOT / "tests/fixtures/fake-cli"
TOOLS = {"codex": ("codex", "gpt-6-luna", "low"),
         "claude-code": ("claude", "claude-sonnet-5-5", "medium"),
         "cursor": ("agent", "composer-2.5", "high")}


class RunFakeTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.work = self.linked()
        self.brief = self.directory / "brief.md"
        self.brief.write_text("Private brief, never echo this.\n", encoding="utf-8")
        self.env.update(CLAUDE_CONFIG_DIR=str(self.home / "claude"),
                        CURSOR_CONFIG_DIR=str(self.home / "cursor"),
                        ORCEXE_STATE_DIR=str(self.home / "state"),
                        XDG_CACHE_HOME=str(self.home / "cache"),
                        PYTHONDONTWRITEBYTECODE="1",
                        PYTHONPATH=str(ROOT / "plugins/orcexe/src"))
        self.counter = 0

    def prepare(self, tool, scenario="success", profile="review", workdir=None, **fake_env):
        self.counter += 1
        name = "case-{}-{}".format(self.counter, tool)
        wrapper = self.directory / (name + ".py")
        calls = self.directory / (name + "-calls.jsonl")
        ready = self.directory / (name + "-ready")
        pid = self.directory / (name + "-pid")
        fake, model, effort = TOOLS[tool]
        settings = dict(ORCEXE_FAKE_SCENARIO=scenario, ORCEXE_FAKE_CALLS=str(calls),
                        ORCEXE_FAKE_READY_FILE=str(ready), **fake_env)
        # The override wrapper sets fixture controls locally. Production never
        # needs to forward test-only environment names to an executor.
        wrapper.write_text(
            "#!" + sys.executable + "\n"
            "import os,sys,runpy\nfrom pathlib import Path\n"
            "os.environ.update({!r})\n"
            "if ('exec' in sys.argv or '-p' in sys.argv):\n"
            "    Path({!r}).write_text(str(os.getpid()))\n"
            "sys.path.insert(0, {!r})\n"
            "runpy.run_path({!r}, run_name='__main__')\n".format(
                settings, str(pid), str(FAKES), str(FAKES / fake)),
            encoding="utf-8")
        wrapper.chmod(0o700)
        env = dict(self.env)
        env["CURSOR_CONFIG_DIR"] = str(self.home / name / "cursor")
        env["CODEX_HOME"] = str(self.home / name / "codex")
        env["ORCEXE_BIN_" + tool.upper().replace("-", "_")] = str(wrapper)
        args = ["run", "--tool", tool, "--model", model, "--effort", effort,
                "--brief", str(self.brief), "--workdir", str(workdir or self.work),
                "--profile", profile, "--run-id", name]
        return args, env, self.main / ".orcexe/runs" / name, calls, ready, pid

    def invoke(self, prepared, *extra, human=False):
        args, env, directory, calls, ready, pid = prepared
        stop, errors = threading.Event(), []
        watcher = threading.Thread(target=self.observe_receipt, args=(directory, stop, errors))
        watcher.start()
        try:
            completed = subprocess.run([sys.executable, "-B", "-m", "orcexe", *args,
                                       *([] if human else ["--json"]), *extra],
                cwd=str(self.directory), env=env, stdin=subprocess.DEVNULL,
                capture_output=True, text=True, timeout=scaled(10))
        finally:
            stop.set()
            watcher.join()
        self.assertFalse(errors, errors)
        self.assertEqual(completed.stderr, "")
        self.assertNotIn("Private brief", completed.stdout)
        if (directory / "result.json").exists():
            result = json.loads((directory / "result.json").read_text())
            validate_run_result(result, run_dir=directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)
            self.assertEqual((directory / "result.json").stat().st_mode & 0o777, 0o600)
            for key in ("events_path", "final_text_path", "stderr_tail_path"):
                self.assertTrue(Path(result[key]).is_file())
            self.assertFalse(list(directory.glob(".result.json-*")))
            if not human:
                self.assertEqual(json.loads(completed.stdout), redact.redact_obj(result))
        else:
            result = None
        return completed, result

    @staticmethod
    def observe_receipt(directory, stop, errors):
        """Readers must see absence or a complete valid result on every path."""
        while not stop.is_set():
            try:
                data = json.loads((directory / "result.json").read_text())
                validate_run_result(data, run_dir=directory)
            except FileNotFoundError:
                pass
            except Exception as error:
                errors.append(error)
            stop.wait(.005)

    def test_executor_exit_matrix_and_signals(self):
        # Each write has its own linked tree. Review cases share a stable tree;
        # every CLI has a private config directory, so cases can run together.
        cases = []
        for tool in TOOLS:
            review = self.prepare(tool)
            cases.append((tool + ":review", review, (), tool == "claude-code", 0, "completed", []))
            if tool == "codex":
                codex_review = review
            if tool == "cursor":
                cursor = Path(review[1]["CURSOR_CONFIG_DIR"]) / "cli-config.json"
                cursor.parent.mkdir(parents=True)
                cursor_original = dict(model="saved", selectedModel="old", other=42,
                    modelSelectionHistory=["old"], modelParameters={"effort": "low"},
                    hasChangedDefaultModel=False)
                cursor.write_text(json.dumps(cursor_original))
            work = self.linked("edit-" + tool)
            (work / "dirty.txt").write_text("pre-existing\n")
            prepared = self.prepare(tool, profile="write", workdir=work,
                ORCEXE_FAKE_EDIT_FILE="tracked.txt", ORCEXE_FAKE_EDIT_TEXT="edited\n")
            cases.append((tool + ":edit", prepared, (), False, 0, "completed", ["tracked.txt"]))
            if tool == "codex":
                codex_edit = prepared
            cases.append((tool + ":empty", self.prepare(tool, profile="write"), (), False, 2, "completed", []))
            for scenario, status, code in (("invalid-model", "failed", 1),
                    ("auth-401" if tool == "codex" else "auth-missing", "auth", 2)):
                cases.append((tool + ":" + status, self.prepare(tool, scenario), (), False, code, status, []))
            for status in (("timeout", "stalled") if tool == "codex" else ("timeout",)):
                short, long = str(scaled(.2) / 60), str(scaled(8) / 60)
                extra = ("--timeout-min", short if status == "timeout" else long,
                         "--stall-min", short if status == "stalled" else long)
                cases.append((tool + ":" + status, self.prepare(tool, "stall"), extra, False, 2, status, []))
            prepared = self.prepare(tool, profile="write", workdir=self.main)
            cases.append((tool + ":main", prepared, (), True, 2, "blocked", []))
            for profile in ("review", "write"):
                prepared = self.prepare(tool, profile=profile, workdir=self.home)
                cases.append((tool + ":non-git-" + profile, prepared, (), False, 2, None, []))
        for tool in ("codex", "claude-code"):
            prepared = self.prepare(tool, "quota", ORCEXE_FAKE_APPSERVER_SCENARIO="quota-exhausted")
            cases.append((tool + ":quota", prepared, (), False, 2, "quota", []))
        for option, value in (("--run-id", "../escape"), ("--run-id", ""), ("--run-id", "x" * 65),
                              ("--brief", str(self.home)), ("--brief", str(self.home / "absent")),
                              ("--workdir", str(self.home / "absent"))):
            cases.append(("usage", self.prepare("codex"), (option, value), False, 2, None, []))
        for name, contents in (("oversize", b"x" * (1024 * 1024 + 1)), ("invalid-utf8", b"\xffprivate")):
            brief = self.directory / name
            brief.write_bytes(contents)
            cases.append((name, self.prepare("codex"), ("--brief", str(brief)), False, 2, None, []))

        # Only the edited Codex run adds a header. Pre-existing trust for the
        # other writes avoids concurrent ledger writes in this test matrix.
        for label, prepared, extra, human, code, status, files in cases:
            args, env, *_ = prepared
            if args[args.index("--tool") + 1] == "codex" and args[args.index("--profile") + 1] == "write":
                home = Path(env["CODEX_HOME"])
                home.mkdir(parents=True)
                workdir = "/pre-existing" if label == "codex:edit" else args[args.index("--workdir") + 1]
                (home / "config.toml").write_text('[projects.' + json.dumps(workdir) + ']\ntrust_level = "trusted"\n')
        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(self.invoke, prepared, *extra, human=human)
                       for label, prepared, extra, human, code, status, files in cases]
            signals = []
            if os.name == "posix":
                for tool in TOOLS:
                    for sig in ((signal.SIGINT, signal.SIGTERM, signal.SIGHUP) if tool == "codex"
                                else (signal.SIGINT, signal.SIGTERM)):
                        signals.append(pool.submit(self.signal_case, self.prepare(tool, "stall"), sig))
            for case, future in zip(cases, futures):
                label, prepared, extra, human, code, status, files = case
                with self.subTest(case=label):
                    completed, result = future.result()
                    self.assertEqual(completed.returncode, code, completed.stdout)
                    if status is None:
                        self.assertIsNone(result)
                        self.assertFalse(prepared[3].exists())
                        self.assertFalse(prepared[2].exists())
                    else:
                        self.assertEqual((result["status"], result["diff_files"]), (status, files))
                    if label.endswith(":edit"):
                        self.assertIn("dirty.txt", result["dirty_before"])
                    if label.endswith(":empty"):
                        self.assertIn("empty_diff", result["warnings"])
                    if label.endswith(":main"):
                        self.assertIn("main_worktree_write_refused", result["warnings"])
                        self.assertIn("git worktree add", completed.stdout)
                        self.assertFalse(prepared[3].exists())
                    if ":non-git-" in label:
                        self.assertIn("orcexe run needs a git repository in v0.1; non-code work "
                                      "arrives with the everyday mode in v0.4", completed.stdout)
                    if label == "codex:quota":
                        calls = [json.loads(line) for line in prepared[3].read_text().splitlines()]
                        self.assertTrue(any(call["argv"] == ["app-server"] for call in calls))
                    if label == "oversize":
                        self.assertIn("1 MiB", completed.stdout)
                    if label == "invalid-utf8":
                        self.assertNotIn("private", completed.stdout)
                    if human and status is not None:
                        for text in ("Status:", "Model requested/actual:", "Duration:", "Changed files:",
                                     "Warnings:", "agent says:", "result.json", "events.jsonl", "final.md"):
                            self.assertIn(text, completed.stdout)
            for future in signals:
                future.result()

        ledger = json.loads((self.main / ".orcexe/trust-ledger.json").read_text())
        trust.validate(ledger)
        self.assertEqual([entry["key"] for entry in ledger["entries"]],
                         [codex_edit[0][codex_edit[0].index("--workdir") + 1]])
        self.assertEqual(ledger["entries"][0]["run_id"], codex_edit[2].name)
        self.assertEqual(json.loads(cursor.read_text()), dict(cursor_original, fakeLastRun="updated"))
        exclude = (self.main / ".git/info/exclude").read_text()
        self.assertIn("/.orcexe/", exclude.splitlines())
        original = (codex_review[2] / "result.json").read_bytes()
        calls = codex_review[3].read_bytes()
        completed, result = self.invoke(codex_review, human=True)
        self.assertEqual(completed.returncode, 2)
        self.assertEqual((codex_review[2] / "result.json").read_bytes(), original)
        self.assertEqual(codex_review[3].read_bytes(), calls)

    def test_default_id_agent_claims_and_unreadable_brief(self):
        prepared = self.prepare("codex")
        args, env, directory, *_ = prepared
        args[-2:] = []
        requests = []

        def claims(adapter, req):
            requests.append(req)
            result = run._minimal_result(req, adapter.manifest.family, "completed", [])
            result.update(exit_code=0, self_report={"exit": "BLOCKED", "needs": ["context"]})
            if req.model.startswith("sk-proj-"):
                result.update(status="failed", exit_code=1, self_report={"exit": "DONE", "needs": []})
            return result

        output = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(run.CodexAdapter, "run", claims), redirect_stdout(output):
            self.assertEqual(cli.main(args), 0)
        req = requests[0]
        self.assertRegex(req.run_id, r"^run-\d{8}T\d{6}-[0-9a-f]{4}$")
        result = json.loads((Path(req.run_dir) / "result.json").read_text())
        self.assertEqual((result["status"], result["self_report"]["exit"]), ("completed", "BLOCKED"))
        self.assertIn('agent says: {"exit": "BLOCKED"', output.getvalue())
        secret = "sk-proj-" + "Fake19Ab_" * 8
        for json_mode in (False, True):
            arguments = self.prepare("codex")[0] + ["--model", secret] + (["--json"] if json_mode else [])
            output = io.StringIO()
            with mock.patch.dict(os.environ, env, clear=True), \
                    mock.patch.object(run.CodexAdapter, "run", claims), redirect_stdout(output):
                self.assertEqual(cli.main(arguments), 1)
            self.assertNotIn(secret, output.getvalue())
            if json_mode:
                self.assertEqual(json.loads(output.getvalue())["status"], "failed")
        open_file = Path.open

        def unreadable(path, *args, **kwargs):
            if path == self.brief:
                raise PermissionError("private diagnostic")
            return open_file(path, *args, **kwargs)

        output = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(Path, "open", unreadable), redirect_stdout(output):
            self.assertEqual(cli.main(args), 2)
        self.assertNotIn("private diagnostic", output.getvalue())

    def test_atomic_receipts_and_cancelled_fallback_restore_handlers(self):
        prepared = self.prepare("codex")
        args, env, directory, *_ = prepared
        handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        replace = os.replace
        publications = []

        def publish(source, target):
            if Path(target).name == "result.json":
                result = json.loads(Path(source).read_text())
                validate_run_result(result, run_dir=directory)
                self.assertEqual(Path(source).parent, directory)
                if Path(target).exists():
                    validate_run_result(json.loads(Path(target).read_text()), run_dir=directory)
                publications.append(result["status"])
            return replace(source, target)

        def interrupted(adapter, req):
            os.kill(os.getpid(), signal.SIGINT)

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(run.CodexAdapter, "run", interrupted), \
                mock.patch.object(fsio.os, "replace", side_effect=publish), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 2)
        self.assertEqual(publications, ["cancelled"])
        self.assertEqual({sig: signal.getsignal(sig) for sig in handlers}, handlers)
        validate_run_result(json.loads((directory / "result.json").read_text()), run_dir=directory)

        # A first signal during atomic publication must also leave cancelled,
        # rather than interrupting the fallback or publishing stale completion.
        args, env, directory, *_ = self.prepare("codex")
        publications.clear()

        def completed(adapter, req):
            result = run._minimal_result(req, adapter.manifest.family, "completed", [])
            result["exit_code"] = 0
            return result

        def signal_on_publish(source, target):
            if Path(target).name == "result.json" and not publications:
                os.kill(os.getpid(), signal.SIGTERM)
            return publish(source, target)

        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(run.CodexAdapter, "run", completed), \
                mock.patch.object(fsio.os, "replace", side_effect=signal_on_publish), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(args), 2)
        self.assertEqual(publications, ["completed", "cancelled"])
        result = json.loads((directory / "result.json").read_text())
        self.assertEqual(result["status"], "cancelled")
        validate_run_result(result, run_dir=directory)
        self.assertEqual({sig: signal.getsignal(sig) for sig in handlers}, handlers)

    def signal_case(self, prepared, sig):
        args, env, directory, calls, ready, pid = prepared
        process = subprocess.Popen([sys.executable, "-B", "-m", "orcexe", *args, "--json"],
            cwd=str(self.directory), env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        child = None
        errors = []
        stop = threading.Event()
        watcher = threading.Thread(target=self.observe_receipt, args=(directory, stop, errors))
        watcher.start()
        try:
            wait_for(lambda: ready.exists() or process.poll() is not None)
            self.assertTrue(ready.exists(), "fake must handshake before signalling")
            child = int(pid.read_text())
            process.send_signal(sig)
            stdout, stderr = process.communicate(timeout=scaled(10))
            self.assertEqual(process.returncode, 2, stderr)
            self.assertEqual(stderr, "")
            result = json.loads(stdout)
            validate_run_result(result, run_dir=directory)
            self.assertEqual(result["status"], "cancelled")
            stored = json.loads((directory / "result.json").read_text())
            self.assertEqual(result, stored)
            with self.assertRaises(ProcessLookupError):
                os.kill(child, 0)
            with self.assertRaises(ProcessLookupError):
                os.killpg(child, 0)
            self.assertFalse(errors, errors)
        finally:
            stop.set()
            watcher.join()
            if child is None and pid.exists():
                child = int(pid.read_text())
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=scaled(5))
            if child is not None:
                try:
                    os.killpg(child, signal.SIGKILL)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    import unittest
    unittest.main()
