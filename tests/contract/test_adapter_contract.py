"""Every registered adapter exercises the same request/evidence invariants."""

from dataclasses import replace
import json
import os
from pathlib import Path
import threading
from unittest import mock

from tests.contract._reference_adapter import ADAPTERS, ADAPTER_OPTIONS, ROOT, expected_outcome
from tests.unit._timing import run_when_ready, scaled, wait_for
from tests.unit.test_gitutil import GitFixture
from orcexe import proc
from orcexe.adapters import Adapter, RunRequest, validate_run_result
from orcexe.manifest import GLOBAL_FORBIDDEN_ARGV, load_manifest


FAKE = ROOT / "tests" / "fixtures" / "fake-cli"
SCENARIO_RESULTS = {}


class AdapterContract:
    """Mixin instantiated once per ADAPTERS entry below (T13 adds Codex)."""

    def setUp(self):
        super().setUp()
        self.seed()
        self.work = self.linked()
        self.calls = self.directory / "calls.jsonl"
        self.brief = self.directory / "source.md"
        self.brief_text = "UNIQUE_BRIEF_CONTENT_" + "Do the task exactly.\n" * 1000
        self.brief.write_text(self.brief_text, encoding="utf-8")
        self.cursor = self.adapter_name == "cursor"
        self.claude = self.adapter_name == "claude-code"
        self.manifest = load_manifest(ROOT / "plugins" / "orcexe" / "adapters" / ("cursor.yaml" if self.cursor else "claude.yaml" if self.claude else "codex.yaml"))
        self.bin_env = self.manifest.data["bin_env"]
        self.exec_token = "-p" if self.claude or self.cursor else "exec"
        self.env.update(ORCEXE_BIN_CLAUDE_CODE=str(FAKE / ("claude.cmd" if os.name == "nt" else "claude")),
                        CLAUDE_CONFIG_DIR=str(self.home / "claude"), ORCEXE_STATE_DIR=str(self.home / "state"))
        self.env.update(ORCEXE_BIN_CODEX=str(FAKE / ("codex.cmd" if os.name == "nt" else "codex")),
                        ORCEXE_FAKE_CALLS=str(self.calls), ORCEXE_FAKE_PYTHON=os.sys.executable,
                        PATH=os.pathsep.join((str(FAKE), self.env["PATH"])),
                        ORCEXE_FAKE_READY_FILE=str(self.directory / "ready"),
                        OPENAI_API_KEY="must-not-inherit", UNRELATED_PARENT="must-not-inherit")
        self.env.update(ORCEXE_BIN_CURSOR=str(FAKE / ("agent.cmd" if os.name == "nt" else "agent")),
                        CURSOR_CONFIG_DIR=str(self.home / "cursor"))
        self.adapter = self.make_adapter()

    def make_adapter(self, scenario="success", **options):
        env = dict(self.env, ORCEXE_FAKE_SCENARIO="quota" if scenario == "quota-unconfirmed" else scenario,
                   ORCEXE_FAKE_APPSERVER_SCENARIO="quota-exhausted" if scenario == "quota" else "normal")
        return self.adapter_class(self.manifest, env=env,
            allow_extra=("ORCEXE_FAKE_SCENARIO", "ORCEXE_FAKE_CALLS", "ORCEXE_FAKE_PYTHON",
                         "ORCEXE_FAKE_APPSERVER_SCENARIO",
                         "ORCEXE_FAKE_READY_FILE", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
                         "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT"),
            detect_timeout_s=scaled(3), kill_grace_s=scaled(.1),
            **dict(ADAPTER_OPTIONS.get(self.adapter_name, {}), **options))

    def request(self, name="run", profile="review", workdir=None):
        return RunRequest(name, "T11", self.manifest.tool,
            "claude-haiku-5-5" if self.cursor else "claude-sonnet-5-5" if self.claude else "gpt-6-luna", "low", profile,
            str(workdir or self.work), str(self.brief),
            str(self.main / ".orcexe" / "runs" / name), timeout_s=scaled(4), stall_s=scaled(3))

    def call_log(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def assert_result(self, req, result):
        validate_run_result(result, run_dir=req.run_dir)
        directory = Path(req.run_dir)
        self.assertEqual(json.loads((directory / "result.json").read_text(encoding="utf-8")), result)
        brief = directory / "brief" / "brief.md" if self.claude or self.cursor else directory / "brief.md"
        self.assertEqual(brief.read_text(encoding="utf-8"), self.brief_text)
        self.assertEqual(brief.stat().st_mode & 0o777, 0o600)
        for field in ("events_path", "stderr_tail_path", "final_text_path"):
            text = Path(result[field]).read_text(encoding="utf-8")
            self.assertNotIn("fake-user@example.invalid", text)
            if field == "events_path":
                for line in text.splitlines():
                    json.loads(line)
        for call in self.call_log():
            argv = call["argv"]
            self.assertFalse(any("UNIQUE_BRIEF_CONTENT_" in token for token in argv))
            for token in argv:
                for forbidden in GLOBAL_FORBIDDEN_ARGV + self.manifest.data["exec"]["forbidden_argv"]:
                    self.assertFalse(token.startswith(forbidden[:-1]) if forbidden.endswith("*")
                                     else token == forbidden)
            if argv[0] == "exec":
                prompt = argv[-3]  # profile extras follow the prompt in this manifest
                self.assertLessEqual(len(prompt), 600)
                self.assertFalse(prompt.startswith("-"))
                if str(directory / "final.md") not in argv:
                    continue  # another attempt in this test's shared call log
                self.assertIn(str(directory / "brief.md"), prompt)
                if req.profile == "review":
                    self.assertEqual(argv[-2:], ["-s", "read-only"])
            elif argv[0] == "-p":
                self.assertEqual(call["cwd"], str(req.workdir))
                self.assertIn("--add-dir", argv)
                if str(directory / "brief") in argv:
                    prompt = argv[1]  # Claude's prompt immediately follows -p.
                    self.assertIn(str(brief), prompt)
                    self.assertLessEqual(len(prompt), 600)
                if req.profile == "review":
                    self.assertEqual(argv[-2:], ["--mode" if self.cursor else "--permission-mode", "plan"])

    def run_scenario(self, scenario, req):
        adapter = self.make_adapter(scenario)
        if scenario != 'stall':
            return adapter.run(req)
        # Observe the stall after the fake has emitted its events and recorded
        # its model call; interpreter startup is not the behavior being timed.
        ready = self.directory / 'ready'
        ready.unlink(missing_ok=True)
        runner = proc.run

        def prepared(argv, **options):
            if len(argv) > 1 and argv[1] == self.exec_token:
                return run_when_ready(runner, argv, ready.exists, self.addCleanup, **options)
            return runner(argv, **options)

        with mock.patch.object(proc, 'run', side_effect=prepared):
            return adapter.run(req)

    def test_all_fake_scenarios(self):
        scenarios = ("success", "write", "invalid-model", "auth-401", "warning-first", "no-final",
                     "quota", "quota-unconfirmed", "stall", "commit", "big-line", "background-child")
        if self.claude:
            scenarios = ("success", "write", "invalid-model", "auth-missing", "quota", "stall", "commit", "model-mismatch", "hooks")
        if self.cursor:
            scenarios = ("success", "write", "invalid-model", "auth-missing", "untrusted",
                         "quota-suspected", "stall", "commit", "model-mismatch", "shell", "shell-blocked", "hooks")
        table = {}
        for scenario in scenarios:
            with self.subTest(scenario=scenario):
                self.calls.unlink(missing_ok=True)
                work = self.linked("scenario-" + scenario)
                (work / "pre-existing.txt").write_text("leave alone", encoding="utf-8")
                profile = "write" if scenario in ("write", "commit") else "review"
                req = self.request(scenario, profile, work)
                if scenario == "stall":
                    req = replace(req, stall_s=scaled(.3))
                result = self.run_scenario(scenario, req)
                expected, warnings = expected_outcome(self.adapter_name, scenario, req)
                if self.cursor and scenario in ('shell', 'shell-blocked'):
                    warnings = warnings + ['shell_used:{}'.format(7 if scenario == 'shell' else 1)]
                self.assertEqual(result["status"], expected)
                self.assertEqual(result["warnings"], warnings)
                self.assert_result(req, result)
                self.assertIn("pre-existing.txt", result["dirty_before"])
                self.assertNotIn("pre-existing.txt", result["diff_files"])
                if scenario in ("write", "commit"):
                    self.assertEqual(result["diff_files"], ["orcexe-fake-output.txt"])
                if scenario == "write":
                    self.assertIn("orcexe-fake-output.txt", result["untracked"])
                if scenario == "big-line":
                    records = Path(result["events_path"]).read_text(encoding="utf-8").splitlines()
                    self.assertIn("_raw", json.loads(records[0]))
                    self.assertLess(len(records[0]), 1100000)
                self.assertEqual(sum(call["model_call"] for call in self.call_log()),
                                 0 if self.cursor and scenario in ("invalid-model", "auth-missing") else 1)
                table[scenario] = (result["status"], result["warnings"])
        SCENARIO_RESULTS[self.adapter_name] = table

    def test_codex_argv_remains_exact_for_both_profiles(self):
        if self.claude or self.cursor:
            return
        for profile, sandbox in (("write", "workspace-write"), ("review", "read-only")):
            req = self.request("argv-" + profile, profile)
            self.calls.unlink(missing_ok=True)
            self.adapter.run(req)
            call, = [call for call in self.call_log() if call["model_call"]]
            directory = Path(req.run_dir).resolve()
            prompt = "Read the task brief at {} and carry it out. Follow its rules exactly.".format(directory / "brief.md")
            expected = ["exec", "-C", str(Path(req.workdir).resolve()), "-m", req.model,
                "-c", "model_reasoning_effort=" + req.effort, "--json", "-o",
                str(directory / "final.md"), prompt, "-s", sandbox]
            self.assertEqual(call["argv"], expected)
            self.assertEqual(json.dumps(call["argv"]).encode(), json.dumps(expected).encode())

    def test_main_worktree_write_refused_and_attempts_append_only(self):
        req = self.request("refused", "write", self.main)
        result = self.adapter.run(req)
        self.assertEqual(result["status"], "blocked")
        self.assertEqual(result["warnings"], ["main_worktree_write_refused", "empty_diff"])
        self.assertEqual(self.call_log(), [])
        self.assert_result(req, result)
        before = (Path(req.run_dir) / "result.json").read_bytes()
        with self.assertRaises(FileExistsError):
            self.adapter.run(req)
        self.assertEqual((Path(req.run_dir) / "result.json").read_bytes(), before)
        req = self.request("once")
        self.adapter.run(req)
        calls = self.call_log()
        with self.assertRaises(FileExistsError):
            self.make_adapter().run(req)
        self.assertEqual(self.call_log(), calls)

    def test_empty_write_diff_and_external_brief_policy(self):
        req = self.request("empty-write", "write")
        result = self.adapter.run(req)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["diff_files"], [])
        self.assertEqual(result["warnings"], expected_outcome(self.adapter_name, "empty-write", req)[1])
        self.assert_result(req, result)
        external = replace(self.request("external", "write"), run_dir=str(self.directory / "external"))
        calls = self.call_log()
        if self.claude or self.cursor:
            self.assertEqual(self.adapter.run(external)["status"], "completed")
        else:
            with self.assertRaisesRegex(ValueError, "read grants"):
                self.adapter.run(external)
            self.assertEqual(self.call_log(), calls)
        internal = replace(self.request("internal", "write"),
                           run_dir=str(self.work / ".orcexe" / "runs" / "internal"))
        result = self.adapter.run(internal)
        self.assertEqual(result["warnings"], ["empty_diff"])
        # Evidence files cannot count as changes even without a gitignore rule.
        unignored = replace(self.request("unignored", "write"),
                            run_dir=str(self.work / "unignored-evidence"))
        result = self.adapter.run(unignored)
        self.assertEqual(result["diff_files"], [])
        self.assertEqual(result["warnings"], ["empty_diff"])

    def test_detect_hints_environment_and_missing_binary(self):
        self.assertEqual(self.adapter.detect(), {"present": True, "version": "2026.11.13-eacade1" if self.cursor else "2.1.310" if self.claude else "0.163.0",
            "path": self.env[self.bin_env], "min_ok": True})
        calls = self.call_log()
        self.assertEqual(self.adapter.login_hint(), [self.env[self.bin_env], *self.manifest.data["auth"]["login_cmd"][1:]])
        self.assertEqual(self.call_log(), calls)
        self.assertEqual(self.adapter.capabilities(), self.manifest.data)
        self.assertNotIn("OPENAI_API_KEY", self.adapter._environment())
        self.assertNotIn("UNRELATED_PARENT", self.adapter._environment())
        self.assertEqual(self.adapter._environment()["HOME"], str(self.home))
        with self.assertRaises(TypeError):
            Adapter(self.manifest)
        with self.assertRaises(TypeError):
            self.adapter_class({})
        for output, version, min_ok in ((("2026.11.12-abc", "2026.11.12-abc", False) if self.cursor else ("codex 0.100.0", "0.100.0", False)),
                                       ("unparseable", None, False)):
            def version_probe(argv, **options):
                options["on_line"](output)
                return proc.ProcResult("exited", 0, scaled(.01), 1, len(output), 0, "", False, None)

            with mock.patch("orcexe.proc.run", side_effect=version_probe):
                found = self.adapter.detect()
            self.assertEqual((found["version"], found["min_ok"]), (version, min_ok))
        path_env = dict(self.env)
        del path_env[self.bin_env]
        # Only fixture PATH search is permitted in this test.
        path_env["PATH"] = str(FAKE)
        searched = self.adapter_class(self.manifest, env=path_env)
        self.assertEqual(searched._binary(), self.env[self.bin_env])
        missing = self.make_adapter()
        missing._env[self.bin_env] = str(self.directory / "nonexistent-cli")
        self.assertEqual(missing.detect(), {"present": False, "version": None, "path": None, "min_ok": False})
        result = missing.run(self.request("spawn-error"))
        self.assertEqual((result["status"], result["exit_code"]), ("failed", None))
        self.assert_result(self.request("spawn-error"), result)

    def test_timeout_and_cancel_runner_priority(self):
        req = replace(self.request("timeout"), timeout_s=scaled(.3), stall_s=scaled(3))
        adapter = self.make_adapter("stall")
        with mock.patch.object(adapter, "_classify", return_value=("completed", [])) as classify:
            result = adapter.run(req)
        self.assertEqual(result["status"], "timeout")
        classify.assert_not_called()
        self.assert_result(req, result)
        marker = self.directory / "ready"
        marker.unlink(missing_ok=True)
        req = replace(self.request("cancel"), timeout_s=scaled(8), stall_s=scaled(5))
        result_box, errors = [], []

        def running():
            try:
                result_box.append(adapter.run(req))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=running)
        thread.start()
        try:
            wait_for(marker.exists, seconds=5)
            self.assertFalse(self.make_adapter().cancel(req.run_id))
            self.assertTrue(adapter.cancel(req.run_id))
        finally:
            adapter.cancel(req.run_id)
            thread.join(timeout=scaled(10))
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(result_box[0]["status"], "cancelled")
        self.assertFalse(adapter.cancel(req.run_id))
        self.assert_result(req, result_box[0])

    def test_redaction_bounded_summary_and_done_never_overrides_failure(self):
        req = self.request("observed-failure")
        adapter = self.make_adapter()
        original_run = proc.run
        captured = {}

        def injected(argv, **options):
            if len(argv) < 2 or argv[1] != self.exec_token:
                return original_run(argv, **options)
            emit = options["on_line"]
            emit(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True}
                            if self.claude or self.cursor else {"type": "turn.failed"}))
            for index in range(80):
                emit(json.dumps({"type": "untrusted-" + str(index)}))
            emit("raw fake-user@example.invalid")
            if self.claude or self.cursor:
                emit(json.dumps({"type": "result", "subtype": "success", "is_error": False,
                    "terminal_reason": "completed", "result": "fake-user@example.invalid\nORCEXE_EXIT: DONE"}))
            else:
                emit(json.dumps({"type": "item.completed", "item": {"type": "agent_message",
                    "text": "fake-user@example.invalid\nORCEXE_EXIT: DONE"}}))
            emit(json.dumps({"type": "turn.completed", "usage": {}}))
            return proc.ProcResult("exited", 1, scaled(.01), 85, 0, 0,
                                   "fake-user@example.invalid", False, None)

        classify = adapter._classify

        def inspected(summary, result):
            captured.update(summary)
            return classify(summary, result)

        with mock.patch("orcexe.proc.run", side_effect=injected), \
                mock.patch.object(adapter, "_classify", side_effect=inspected):
            result = adapter.run(req)
        self.assertEqual((result["status"], result["self_report"]["exit"]), ("failed", "DONE"))
        self.assertLessEqual(len(captured["events"]), 32)
        self.assertLessEqual(len(captured["counts"]), len(self.manifest.data["events"]["types"]) + 1)
        self.assertEqual(captured["counts"]["result" if self.claude or self.cursor else "turn.failed"],
                         2 if self.claude or self.cursor else 1)
        self.assert_result(req, result)
        records = [json.loads(line) for line in Path(result["events_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any("_raw" in event for event in records))

    def test_model_mismatch_and_nonzero_exit_cannot_complete(self):
        req = self.request("mismatch")
        with mock.patch.object(self.adapter, "_extract", return_value={
                "final_text": "ORCEXE_EXIT: BLOCKED", "usage": {}, "model_actual": "other"}):
            result = self.adapter.run(req)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["warnings"], ["model_mismatch"])
        self.assertEqual(result["self_report"]["exit"], "BLOCKED")
        req = self.request("nonzero")
        adapter = self.make_adapter("invalid-model")
        with mock.patch.object(adapter, "_classify", return_value=("completed", [])):
            result = adapter.run(req)
        self.assertEqual(result["status"], "failed")
        self.assert_result(req, result)


for _name, _adapter in ADAPTERS.items():
    globals()["AdapterContract_" + _name] = type("AdapterContract_" + _name,
        (AdapterContract, GitFixture), {"adapter_name": _name, "adapter_class": _adapter})
