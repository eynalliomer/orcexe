"""Offline diagnostics: synthetic accounts, isolated homes, no model calls."""

import copy
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from orcexe import cli, doctor, fsio, proc, trust

ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tests/fixtures/fake-cli"
EXTRAS = ("ORCEXE_FAKE_CALLS", "ORCEXE_FAKE_SCENARIO", "ORCEXE_FAKE_APPSERVER_SCENARIO", "ORCEXE_FAKE_PYTHON")
PII = ("fake-user@example.invalid", "fake-acct", "fake-claude-org-id", "fake-claude-org-name",
       "claude-fake@example.invalid", "fake-cursor-userId", "fake-cursor-firstName",
       "fake-cursor-lastName", "workspaceRouting", "fake-workspace")


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="doctor-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.calls = self.root / "calls.jsonl"
        self.env = {"PATH": os.environ.get("PATH", os.defpath), "HOME": str(self.home),
                    "USERPROFILE": str(self.home), "CODEX_HOME": str(self.home / "codex"),
                    "CLAUDE_CONFIG_DIR": str(self.home / "claude"), "CURSOR_CONFIG_DIR": str(self.home / "cursor"),
                    "ORCEXE_STATE_DIR": str(self.home / "state"), "ORCEXE_FAKE_CALLS": str(self.calls),
                    "ORCEXE_FAKE_PYTHON": sys.executable}
        for tool, binary in zip(doctor.TOOLS, ("codex", "claude", "agent")):
            self.env["ORCEXE_BIN_" + tool.upper().replace("-", "_")] = str(FAKE / (binary + ".cmd" if os.name == "nt" else binary))
        patch = mock.patch.dict(os.environ, self.env, clear=True)
        patch.start()
        self.addCleanup(patch.stop)
        original = doctor._adapter
        def adapter(tool, quick=False, **options):
            result = original(tool, quick, **options)
            result._allow_extra = EXTRAS
            return result
        patch = mock.patch.object(doctor, "_adapter", side_effect=adapter)
        patch.start()
        self.addCleanup(patch.stop)
        patch = mock.patch.object(doctor, "_main_worktree", return_value=None)
        patch.start()
        self.addCleanup(patch.stop)

    def log(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def run_doctor(self, **options):
        options.setdefault("budget_scale", .1)
        return doctor.run_doctor(repo=self.root, **options)

    def invoke(self, *arguments):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(list(arguments))
        return code, output.getvalue()

    def assert_private(self, value):
        for forbidden in (*doctor.FORBIDDEN, *PII, getattr(self, "secret", "sk-ant-test-never-present")):
            self.assertNotIn(forbidden, value)

    def assert_model_free(self):
        calls = self.log()
        self.assertTrue(calls)
        self.assertTrue(all(not call["model_call"] and "exec" not in call["argv"] and "-p" not in call["argv"] for call in calls))

    def test_three_tool_status_table(self):
        for tool in doctor.TOOLS:
            for scenario, expected in (("success", "ok"), ("auth-missing", "missing"), ("absent", "unknown")):
                with self.subTest(tool=tool, scenario=scenario):
                    override = "ORCEXE_BIN_" + tool.upper().replace("-", "_")
                    env = {"ORCEXE_FAKE_SCENARIO": scenario if scenario != "absent" else "success"}
                    if scenario == "absent":
                        env[override] = str(self.root / "absent")
                    with mock.patch.dict(os.environ, env):
                        data, cached = self.run_doctor(tool=tool, no_cache=True)
                    self.assertFalse(cached)
                    self.assertEqual(data["tools"][0]["auth"], expected)
                    self.assertEqual(data["tools"][0]["present"], scenario != "absent")
                    check = {c["id"]: c for c in data["checks"]}
                    self.assertEqual(check[tool + ".detect"]["status"], "info" if scenario == "absent" else "pass")
                    if scenario == "auth-missing":
                        self.assertEqual(check[tool + ".auth"]["status"], "warn")
                        self.assertIn("login", check[tool + ".auth"]["fix"])
                    self.assertEqual(data["summary"]["fail"], 0 if scenario == "success" else 1)
                    for c in data["checks"]:
                        if c["status"] in ("warn", "fail"):
                            self.assertTrue(c["fix"])
        self.assert_model_free()

    def test_slow_cursor_models_and_dict_projection(self):
        scale = .05
        actual = proc.run
        observed = []
        ids = ["model-{}".format(n) for n in range(151)]
        def slow_models(argv, **options):
            if argv[1:] == ["models"]:
                observed.append(options["timeout_s"])
                script = "import time; time.sleep({}); print({!r})".format(
                    1.5 * scale, "\n".join(name + " - Synthetic" for name in ids))
                argv = [sys.executable, "-B", "-c", script]
            return actual(argv, **options)
        with mock.patch.object(doctor.proc, "run", side_effect=slow_models):
            data, _ = self.run_doctor(tool="cursor", no_cache=True, budget_scale=scale)
        self.assertEqual(data["tools"][0]["models"], ids)
        self.assertIn("INFO  cursor: 151 model ids", doctor.human_report(data))
        self.assertEqual(len(observed), 1)
        self.assertGreater(observed[0], 1.5 * scale)
        self.assertEqual(doctor._models({"models": ids}), ids)
        cursor = [{"id": name, "display": "Synthetic", "supported_efforts": ["high"]} for name in ids]
        self.assertEqual(doctor._models({"models": cursor + cursor[:1]}), ids)
        self.assertIsNone(doctor._models({"models": [{"display": "missing id"}]}))
        self.assert_model_free()

    def test_stalled_fake_timeout_warning_for_each_probe(self):
        actual = proc.run
        commands = (["--version"], ["status", "--format", "json"], ["models"])
        def stall(argv, **options):
            if argv[1:] in commands:
                argv = [sys.executable, "-B", "-c", "import time; time.sleep(30)"]
            return actual(argv, **options)
        # One report exercises every stalled operation, with no repeated setup
        # or unrelated successful subprocesses between the timeout assertions.
        with mock.patch.object(doctor.proc, "run", side_effect=stall):
            data, _ = self.run_doctor(tool="cursor", no_cache=True, budget_scale=.005)
        for operation in ("detect", "auth", "models"):
            check = next(c for c in data["checks"] if c["id"] == "cursor." + operation)
            self.assertEqual(check["status"], "warn")
            self.assertIn("timed out after 0.05 s", check["message"])
            self.assertEqual(check["fix"], "re-run `orcexe doctor --no-cache`; if it persists run `agent --version` yourself")
            self.assertIn("WARN  " + check["message"], doctor.human_report(data))

    def test_auth_subcommands_share_budget_and_appserver_timeouts_are_visible(self):
        actual = proc.run
        auth_budgets = []
        def slow_status(argv, **options):
            if argv[1:] == ["status", "--format", "json"]:
                time.sleep(.08)
            if argv[1:2] in (["status"], ["about"]):
                auth_budgets.append(options["timeout_s"])
            return actual(argv, **options)
        with mock.patch.object(doctor.proc, "run", side_effect=slow_status):
            doctor._call(doctor._adapter("cursor", budget_scale=.1), "auth_status")
        self.assertEqual(len(auth_budgets), 2)
        self.assertLess(auth_budgets[1], auth_budgets[0] - .05)
        # The adapter normally swallows plan RPC failures. Doctor must still
        # surface the shared deadline for auth, model listing and quota.
        popen = subprocess.Popen
        def stall_appserver(argv, **options):
            if argv[1:] == ["app-server"]:
                argv = [sys.executable, "-B", "-c", "import time; time.sleep(30)"]
            return popen(argv, **options)
        def login_status(argv, **options):
            if argv[1:] == ["login", "status"]:
                return proc.ProcResult("exited", 0, 0, 0, 0, 0, "", False, None)
            return actual(argv, **options)
        with mock.patch("orcexe.adapters.codex_appserver.subprocess.Popen", side_effect=stall_appserver) as calls, \
                mock.patch.object(doctor.proc, "run", side_effect=login_status), \
                mock.patch.dict(doctor._BUDGET_S, total=600):
            # Exercise all three per-probe deadlines even under parallel load.
            # The overall deadline is covered by test_total_budget_limits_later_probes.
            data, _ = self.run_doctor(tool="codex", no_cache=True, budget_scale=.005)
        self.assertEqual(sum(call.args[0][1:] == ["app-server"] for call in calls.call_args_list), 3)
        for operation in ("auth", "models", "quota"):
            check = next(c for c in data["checks"] if c["id"] == "codex." + operation)
            self.assertEqual(check["status"], "warn")
            self.assertIn("timed out after 0.05 s", check["message"])
            self.assertIn("`codex --version`", check["fix"])

    def test_default_budgets_and_parallel_tools(self):
        self.assertEqual({name: doctor._BUDGET_S[name] for name in
                          ("detect", "auth", "models", "appserver", "quota", "total")},
                         dict(detect=10, auth=10, models=10, appserver=10, quota=10, total=60))
        barrier = threading.Barrier(3, timeout=2)
        def detect():
            barrier.wait()
            return {"present": False}
        adapters = [doctor._adapter(tool) for tool in doctor.TOOLS]
        for adapter in adapters:
            adapter.detect = detect
        with mock.patch.object(doctor, "_adapter", side_effect=adapters):
            data, _ = self.run_doctor(no_cache=True)
        self.assertEqual(len(data["tools"]), 3)
        self.assertTrue(all(c["status"] == "info" for c in data["checks"] if c["id"].endswith(".detect")))

    def test_timeout_message_uses_configured_probe_budget(self):
        for deadline in (10.01, 10):
            with self.subTest(deadline=deadline):
                adapter = doctor._adapter("cursor", budget_scale=.005)
                deadlines = []

                def timeout():
                    deadlines.append(adapter._doctor_deadline)
                    raise TimeoutError()

                with mock.patch.object(adapter, "auth_status", side_effect=timeout), \
                        mock.patch.object(doctor.time, "monotonic", side_effect=(10, 10.02)):
                    raw = doctor._call(adapter, "auth_status", deadline)
                self.assertEqual(deadlines, [deadline] if deadline > 10 else [])
                self.assertEqual(raw["_timeout_s"], .05)
                self.assertEqual(doctor._timeout_line("cursor", "auth", raw)["message"],
                                 "cursor: auth timed out after 0.05 s")

    def test_total_budget_limits_later_probes(self):
        adapter = doctor._adapter("cursor")
        adapter._doctor_budgets = dict(doctor._BUDGET_S, detect=.4, auth=.4, models=.4, quota=.4)
        actual = proc.run
        budgets = []
        def stall(argv, **options):
            if Path(argv[0]).name == "agent":
                budgets.append(options["timeout_s"])
                argv = [sys.executable, "-B", "-c", "import time; time.sleep(30)"]
            return actual(argv, **options)
        started = time.monotonic()
        with mock.patch.object(doctor.proc, "run", side_effect=stall):
            _, checks = doctor._tool_report(adapter, False, None, started + .15)
        self.assertEqual(len(budgets), 1)  # No launches after the total deadline.
        self.assertLessEqual(budgets[0], .15)
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(next(c for c in checks if c["id"] == "cursor.detect")["status"], "warn")

    def test_anthropic_host_base_url_only_is_informational(self):
        for host in (False, True):
            env = {name: "private-value" for name in
                   ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")}
            if host:
                env["CLAUDECODE"] = ""  # Presence, even with an empty value.
            with self.subTest(host=host), mock.patch.dict(os.environ, env):
                data, _ = self.run_doctor(tool="claude-code", quick=True, no_cache=True)
                adapter = doctor._adapter("claude-code")
            checks = {c["id"]: c for c in data["checks"]}
            base = checks["env.anthropic_base_url"]
            self.assertEqual(base["status"], "info" if host else "warn")
            if host:
                self.assertEqual(base["message"], "ANTHROPIC_BASE_URL set by the Claude Code host; orcexe never passes it to child tools")
                self.assertIsNone(base["fix"])
            for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
                self.assertEqual(checks["env." + key.lower()]["status"], "warn")
            for key in env:
                self.assertNotIn(key, adapter._env)
                self.assertNotIn(key, adapter._environment())
            self.assertNotIn("private-value", json.dumps(data))

    def test_cache_no_processes_refresh_corrupt_and_selection(self):
        data, _ = self.run_doctor(quick=True)
        before = self.calls.read_bytes()
        with mock.patch.object(doctor.proc, "run", side_effect=AssertionError("cached must not spawn")):
            cached, used = self.run_doctor(quick=True)
        self.assertTrue(used)
        self.assertEqual(cached, data)
        self.assertEqual(self.calls.read_bytes(), before)
        self.assertIn("cached: true", doctor.human_report(cached, True))
        self.assertNotIn("cached", cached)
        self.run_doctor(quick=True, no_cache=True)
        self.assertGreater(len(self.calls.read_bytes()), len(before))
        doctor._cache_path().write_text("{corrupt")
        self.assertFalse(self.run_doctor(quick=True)[1])
        self.assertFalse(self.run_doctor(quick=True, tool="cursor")[1])
        data, _ = self.run_doctor(quick=True)
        data["checked_at"] = "2000-01-01T00:00:00Z"
        fsio.atomic_write_json(doctor._cache_path(), data)
        self.assertFalse(self.run_doctor(quick=True)[1])
        self.assert_model_free()

    def test_full_report_privacy_atomic_mode_quick_and_probe_commands(self):
        self.secret = "sk-ant-test-" + "synthetic" * 4
        with mock.patch.dict(os.environ, ANTHROPIC_API_KEY=self.secret, CLAUDECODE="private-host-value"):
            code, output = self.invoke("doctor", "--json", "--no-cache")
            self.assertEqual(code, 0, output)
            data = json.loads(output)
            doctor.validate_env(data)
            self.assertEqual([v["tool"] for v in data["tools"]], list(doctor.TOOLS))
            self.assertEqual(data["tools"][0]["plan"], "plus")
            self.assertEqual(data["tools"][0]["quota"]["source"], "app-server")
            self.assertIsNone(data["tools"][1]["models"])
            self.assertIn("composer-2.5", data["tools"][2]["models"])
            self.assertIn("E1", data["tools"][0]["side_effects"])
            self.assertIn("C1", data["tools"][2]["side_effects"])
            self.assertEqual(data["host"]["capabilities"][0]["evidence"], "measured")
            for text in (output, doctor._cache_path().read_text(), self.invoke("doctor")[1]):
                self.assert_private(text)
            if os.name != "nt":
                self.assertEqual(stat.S_IMODE(doctor._cache_path().stat().st_mode), 0o600)
            # A quick refresh may use old model ids but must not start app-server.
            before = len(self.log())
            quick, _ = self.run_doctor(quick=True, no_cache=True)
            self.assertEqual(quick["tools"][0]["models"], data["tools"][0]["models"])
            self.assertTrue(all(c["argv"] != ["app-server"] for c in self.log()[before:]))
            for tool in doctor.TOOLS:
                for operation in ("detect", "auth", "models", "quota"):
                    code, text = self.invoke("probe", tool, operation, "--json")
                    self.assertEqual(code, 1 if (tool, operation) == ("claude-code", "models") else 0, text)
                    self.assert_private(text)
                    self.assert_private(self.invoke("probe", tool, operation)[1])
        self.assert_model_free()

    def test_validate_mutations(self):
        data, _ = self.run_doctor(tool="claude-code", quick=True)
        def missing(d):
            del d["python"]
        def extra(d):
            d["cached"] = True
        def status(d):
            d["checks"][0]["status"] = "good"
        def no_fix(d):
            d["checks"][0].update(status="warn", fix=None)
        def forbidden(d):
            d["tools"][0]["quota"]["orgId"] = "synthetic"
        def bool_number(d):
            d["summary"]["fail"] = False
        def bad_quota(d):
            d["tools"][0]["quota"]["windows"] = [{"name": "primary", "used_pct": "bad", "resets_at": 1}]
        for mutation in (missing, extra, status, no_fix, forbidden, bool_number, bad_quota):
            changed = copy.deepcopy(data)
            mutation(changed)
            with self.subTest(mutation=mutation.__name__), self.assertRaises(ValueError):
                doctor.validate_env(changed)

    def test_prerequisites_disk_repo_git_suffix_and_failures(self):
        for text, expected in (("git version 2.54.0.windows.1", (2, 54, 0)), ("git version 2.31.0 (Apple Git-1)", (2, 31, 0)), ("nonsense", None)):
            self.assertEqual(doctor.git_version(text), expected)
        with mock.patch.object(doctor.shutil, "disk_usage", return_value=type("Disk", (), {"free": 1})()) as usage, \
                mock.patch.object(doctor.platform, "system", return_value="AlienOS"):
            checks, _, _, _ = doctor._prerequisites(self.root)
        usage.assert_called_once_with(self.root)
        self.assertEqual(next(c for c in checks if c["id"] == "disk")["status"], "warn")
        self.assertEqual(next(c for c in checks if c["id"] == "os")["status"], "fail")
        def old_git(argv, **options):
            options["on_line"]("git version 2.30.9")
            return proc.ProcResult("exited", 0, 0, 0, 0, 0, "", False, None)
        with mock.patch.object(doctor.proc, "run", side_effect=old_git):
            checks, _, _, _ = doctor._prerequisites(self.root)
        self.assertEqual(next(c for c in checks if c["id"] == "git.worktree")["status"], "fail")

    def test_environment_presence_does_not_read_values(self):
        class NamesOnly(dict):
            def __getitem__(self, key):
                if key in (*doctor._ENV_WARN, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CODEX_THREAD_ID", "CODEX_SANDBOX"):
                    raise AssertionError("secret read")
                return super().__getitem__(key)
        env = NamesOnly(dict(os.environ, **{name: "ignored" for name in
                        (*doctor._ENV_WARN, "CLAUDECODE", "CODEX_THREAD_ID", "CODEX_SANDBOX")}))
        with mock.patch.object(doctor.os, "environ", env):
            data, _ = self.run_doctor(tool="claude-code", quick=True, no_cache=True)
        ids = {c["id"] for c in data["checks"]}
        self.assertTrue(all("env." + name.lower() in ids for name in doctor._ENV_WARN))
        self.assertEqual(data["host"]["tool"], "codex")
        self.assertTrue(all(c["status"] == "info" for c in data["host"]["capabilities"]))
        doctor.validate_env(data)

    def test_probe_calls_only_requested_method_and_rejects_unknown_quota(self):
        adapter = doctor._adapter("cursor")
        methods = ("detect", "auth_status", "list_models", "quota")
        for operation, method in zip(("detect", "auth", "models", "quota"), methods):
            with self.subTest(operation=operation), \
                    mock.patch.object(doctor, "_adapter", return_value=adapter), \
                    mock.patch.multiple(adapter, **{name: mock.DEFAULT for name in methods}) as calls:
                for call in calls.values():
                    call.return_value = {"orgId": "private-org", "accountId": "private-account"}
                result, code = doctor.run_probe("cursor", operation)
                self.assertEqual(code, 2)
                calls[method].assert_called_once_with()
                for other in set(methods) - {method}:
                    calls[other].assert_not_called()
                self.assert_private(json.dumps(result))

    def test_trust_counts_without_paths_or_changes(self):
        codex_home = Path(os.environ["CODEX_HOME"])
        codex_home.mkdir()
        key = str(self.root / "private-missing-directory")
        config = codex_home / "config.toml"
        config.write_text('[projects.' + json.dumps(key) + ']\ntrust_level = "trusted"\n')
        ledger = self.root / ".orcexe/trust-ledger.json"
        trust.record(ledger, frozenset(), frozenset({key}), "fixture", config)
        projects = trust.projects_path()
        projects.mkdir(parents=True)
        slug = trust.project_slug(self.root / "cursor-missing")
        (projects / slug).mkdir()
        trust.record(ledger, frozenset(), frozenset({slug}), "cursor-fixture", projects,
                     tool="cursor", workspaces=[self.root / "cursor-missing"])
        before = (ledger.read_bytes(), config.read_bytes())
        with mock.patch.object(doctor, "_main_worktree", return_value=self.root):
            check = doctor._trust_check(self.root)
        self.assertIn("1 present entries; 1 missing directories", check["message"])
        self.assertIn("1 recorded Cursor project dirs", check["message"])
        self.assertEqual(check["fix"], "orcexe trust clean")
        self.assertNotIn(key, json.dumps(check))
        self.assertEqual((ledger.read_bytes(), config.read_bytes()), before)

    def test_exit_codes_missing_manifests_crashes_and_probe_errors(self):
        missing = {"ORCEXE_BIN_" + t.upper().replace("-", "_"): str(self.root / "absent") for t in doctor.TOOLS}
        with mock.patch.dict(os.environ, missing):
            code, text = self.invoke("doctor", "--no-cache", "--quick")
            self.assertEqual(code, 1)
            self.assertIn("install and log in to at least one of codex / claude / agent", text)
            for tool in doctor.TOOLS:
                for operation in ("detect", "auth", "models", "quota"):
                    self.assertEqual(self.invoke("probe", tool, operation)[0], 2)
        for failure in (OSError("fake-user@example.invalid"), RuntimeError("fake-acct")):
            with mock.patch.object(doctor, "_adapter", side_effect=failure):
                for command in (("doctor", "--no-cache"), ("probe", "codex", "detect")):
                    code, text = self.invoke(*command, "--json")
                    self.assertEqual(code, 2)
                    self.assert_private(text)
                    self.assertNotIn("Traceback", text)
        adapter = doctor._adapter("codex")
        with mock.patch.object(doctor, "_adapter", return_value=adapter), \
                mock.patch.object(adapter, "list_models", return_value={"models": None, "error": "app-server error"}) as call:
            result, code = doctor.run_probe("codex", "models")
        call.assert_called_once_with()
        self.assertEqual(code, 1)
        self.assertEqual(result["error"], "probe failed")


if __name__ == "__main__":
    unittest.main()
