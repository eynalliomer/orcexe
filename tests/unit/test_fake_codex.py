"""Exercise the external Codex fixture through the production process runner.

Provenance: T10 §5 is the measurement record; §3 specifies deterministic test
values. Tests named synthetic cover unmeasured validation or injected faults.
No test invokes real Codex or uses the user's home/configuration.
"""

import ast
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

from tests.unit._timing import run_when_ready, scaled


ROOT = Path(__file__).resolve().parents[2]
FAKE = ROOT / "tests" / "fixtures" / "fake-cli"
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe.proc import run


# proc.run intentionally supplies DEVNULL stdin. This local client owns the
# protocol pipe, while run still observes the real fixture's stdout/stderr.
RPC_CLIENT = """
import json, subprocess, sys
argv, lines = json.loads(sys.argv[1]), json.loads(sys.argv[2])
with subprocess.Popen(argv, stdin=subprocess.PIPE, text=True, encoding='utf-8') as child:
    child.communicate(''.join(line + '\\n' for line in lines), timeout=%r)
    sys.exit(child.returncode)
""" % scaled(3)


class FakeCodexTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="orcexe-fake-test-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name).resolve()
        self.work = self.directory / "work"
        self.work.mkdir()
        self.home = self.directory / "home"
        self.home.mkdir()
        self.codex_home = self.directory / "codex-home"
        self.calls = self.directory / "calls.jsonl"
        self.git = shutil.which("git")
        paths = [str(FAKE), str(Path(sys.executable).parent)]
        if self.git:
            paths.append(str(Path(self.git).parent))
        paths.append(os.defpath)
        config = self.directory / "empty-gitconfig"
        config.touch()
        template = self.directory / "empty-git-template"
        template.mkdir()
        self.env = {
            "PATH": os.pathsep.join(paths),
            "HOME": str(self.home), "USERPROFILE": str(self.home),
            "XDG_CONFIG_HOME": str(self.home / ".config"),
            "CODEX_HOME": str(self.codex_home),
            "ORCEXE_FAKE_SCENARIO": "success",
            "ORCEXE_FAKE_CALLS": str(self.calls),
            "ORCEXE_FAKE_PYTHON": sys.executable,
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1",
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
            "GIT_CONFIG_SYSTEM": str(config), "GIT_TERMINAL_PROMPT": "0",
            "GIT_TEMPLATE_DIR": str(template),
            "GIT_AUTHOR_NAME": "Fake Test", "GIT_AUTHOR_EMAIL": "fake@example.invalid",
            "GIT_COMMITTER_NAME": "Fake Test", "GIT_COMMITTER_EMAIL": "fake@example.invalid",
        }
        for name in ("SYSTEMROOT", "COMSPEC", "WINDIR"):
            if name in os.environ:
                self.env[name] = os.environ[name]
        self.command = ["codex.cmd" if os.name == "nt" else "codex"]
        self.assertEqual(Path(shutil.which(self.command[0], path=self.env["PATH"])).resolve(),
                         (FAKE / self.command[0]).resolve())

    def git_run(self, *args):
        if not self.git:
            self.skipTest("git is unavailable")
        return subprocess.run([self.git] + list(args), cwd=str(self.work), env=self.env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, check=True).stdout.decode().strip()

    def repository(self):
        self.git_run("init", "--quiet")

    def invoke(self, args, scenario="success", env=None, ready=False, **options):
        environment = dict(self.env)
        environment["ORCEXE_FAKE_SCENARIO"] = scenario
        if env:
            for key, value in env.items():
                if value is None:
                    environment.pop(key, None)
                else:
                    environment[key] = value
        lines, errors = [], []
        kwargs = dict(cwd=str(self.work), env=environment, timeout_s=scaled(4),
                      stall_s=None, kill_grace_s=scaled(.15),
                      on_line=lines.append, on_stderr_line=errors.append)
        kwargs.update(options)
        argv = self.command + list(args)
        if ready:
            marker = self.directory / "ready"
            marker.unlink(missing_ok=True)
            environment["ORCEXE_FAKE_READY_FILE"] = str(marker)
            result = run_when_ready(run, argv, marker.exists, self.addCleanup, **kwargs)
        else:
            result = run(argv, **kwargs)
        return result, lines, errors

    def execute(self, scenario="success", extra=(), **options):
        return self.invoke(["exec", "-C", str(self.work), "--json"] + list(extra) + ["-"],
                           scenario, **options)

    def events(self, lines):
        values = [json.loads(line) for line in lines]
        self.assertTrue(all("_fake" not in value for value in values))
        return values

    def assert_types(self, lines, types):
        values = self.events(lines)
        self.assertEqual([value["type"] for value in values], types)
        return values

    def assert_success(self, result, lines):
        self.assertEqual((result.status, result.returncode), ("exited", 0))
        values = self.assert_types(lines, ["thread.started", "turn.started",
                                          "item.completed", "turn.completed"])
        self.assertEqual(values[2]["item"]["text"], "OK")
        self.assertEqual(values[2]["item"]["type"], "agent_message")
        self.assertEqual(values[-1]["usage"], {
            "input_tokens": 17296, "cached_input_tokens": 11520,
            "cache_write_input_tokens": 0, "output_tokens": 5,
            "reasoning_output_tokens": 0})
        return values

    def log(self):
        return [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]

    def assert_call(self, model_call, scenario="success", argv=None, index=-1):
        entry = self.log()[index]
        self.assertEqual(set(entry), {"argv", "cwd", "scenario", "model_call"})
        self.assertEqual(entry["cwd"], str(self.work))
        self.assertEqual(entry["scenario"], scenario)
        self.assertIs(entry["model_call"], model_call)
        if argv is not None:
            self.assertEqual(entry["argv"], argv)

    def assert_no_trust(self):
        self.assertFalse((self.codex_home / "config.toml").exists())
        self.assertFalse((self.home / ".codex").exists())

    def test_version_and_default_scenario_do_not_call_model(self):
        result, lines, errors = self.invoke(["--version"], env={"ORCEXE_FAKE_SCENARIO": None})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["codex-cli 0.163.0 (orcexe-fake)"])
        self.assertEqual(errors, [])
        self.assert_call(False, argv=["--version"])
        self.assert_no_trust()

    def test_measured_login_status_on_stderr(self):
        # Logged-in stream/exit measured 2026-11-16 (codex 0.163.0).
        # Missing-login text remains synthetic fault injection.
        for scenario, text, code in (("success", "Logged in using ChatGPT", 0),
                                     ("auth-missing", "Not logged in", 1)):
            with self.subTest(scenario=scenario):
                result, lines, errors = self.invoke(["login", "status"], scenario)
                self.assertEqual(result.returncode, code)
                self.assertEqual(lines, [])
                self.assertEqual(errors, [text])
                self.assert_call(False, scenario, ["login", "status"])
        self.assert_no_trust()

    def test_synthetic_unknown_scenario(self):
        result, lines, errors = self.execute("not-a-scenario", ["-s", "workspace-write"])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(lines, [])
        self.assertEqual(errors, ["orcexe-fake: unknown scenario not-a-scenario"])
        self.assert_call(False, "not-a-scenario")
        self.assert_no_trust()

    def test_unknown_flags_fail_before_model_or_trust(self):
        for flag in ("--bogus-flag", "--sandbox", "--model", "-x", "--json=true"):
            with self.subTest(flag=flag):
                result, lines, errors = self.execute(extra=["-s", "workspace-write", flag])
                self.assertEqual(result.returncode, 2)
                self.assertEqual(lines, [])
                self.assertEqual(errors, ["error: unexpected argument '{}' found".format(flag),
                                          "Usage: codex exec [OPTIONS] [PROMPT]"])
                self.assert_call(False)
        self.assert_no_trust()

    def test_synthetic_strict_value_and_positional_validation(self):
        invalid = [["-m"], ["-C"], ["-o"], ["-s"], ["-c"],
                   ["-c", "no-equals"], ["-c", "=empty-key"],
                   ["-s", "unsafe"], ["first", "second"]]
        for args in invalid:
            with self.subTest(args=args):
                result, lines, errors = self.invoke(["exec"] + args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(lines, [])
                self.assertTrue(errors[0].startswith("error:"))
                self.assertEqual(len(errors), 2)
                self.assert_call(False)
        self.assert_no_trust()

    def test_missing_directory_never_calls_model(self):
        result, lines, errors = self.invoke([
            "exec", "-C", str(self.work / "missing"), "--json",
            "-s", "workspace-write", "prompt"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(lines, [])
        self.assertEqual(errors, ["Error: No such file or directory (os error 2)"])
        self.assert_call(False)
        self.assert_no_trust()

    def test_synthetic_non_git_rejection(self):
        result, lines, errors = self.execute(extra=["-s", "workspace-write"])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(lines, [])
        self.assertEqual(errors, ["orcexe-fake: not inside a git repository"])
        self.assert_call(False)
        self.assert_no_trust()

    def test_success_default_read_only_and_stdin_eof(self):
        self.repository()
        output = self.directory / "answer.txt"
        args = ["exec", "-C", str(self.work), "-m", "gpt-6.1-sol",
                "-c", "one=1", "-c", 'two="value"', "--json", "-o", str(output), "-"]
        result, lines, errors = self.invoke(args)
        self.assert_success(result, lines)
        self.assertEqual(errors, [])
        self.assertEqual(output.read_bytes(), b"OK")
        self.assert_call(True, argv=args)
        self.assert_no_trust()

    def test_plain_stdout_and_relative_output(self):
        self.repository()
        result, lines, errors = self.invoke(["exec", "-C", str(self.work),
                                            "-o", "answer.txt", "say OK"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["OK"])
        self.assertEqual(errors, [])
        self.assertEqual((self.work / "answer.txt").read_bytes(), b"OK")
        self.assert_call(True)

    def test_trust_precedes_auth_failure_and_preserves_config(self):
        self.repository()
        self.codex_home.mkdir()
        config = self.codex_home / "config.toml"
        config.write_text("# retained\n", encoding="utf-8")
        header = '[projects.{}]'.format(json.dumps(str(self.work), ensure_ascii=False))
        for scenario in ("auth-401", "success"):
            observed = []

            def observe(line):
                self.assertIn(header, config.read_text(encoding="utf-8"))
                observed.append(line)

            result, _, _ = self.execute(scenario, ["-s", "workspace-write"], on_line=observe)
            self.assertEqual(result.returncode, int(scenario == "auth-401"))
            self.assertEqual(json.loads(observed[0])["type"], "thread.started")
            self.assert_call(True, scenario)
        self.assertEqual(config.read_text(encoding="utf-8"),
                         '# retained\n\n{}\ntrust_level = "trusted"\n'.format(header))

    def test_trust_finds_parent_git_and_records_actual_invocation_cwd(self):
        self.repository()
        nested = self.work / "nested"
        nested.mkdir()
        result, lines, errors = self.invoke(["exec", "-C", str(nested), "--json",
                                            "-s", "workspace-write", "prompt"])
        self.assert_success(result, lines)
        self.assertEqual(errors, [])
        self.assertIn('[projects.{}]'.format(json.dumps(str(nested))),
                      (self.codex_home / "config.toml").read_text(encoding="utf-8"))
        self.assert_call(True)

    def test_skip_git_check_outside_repo_has_no_trust_write(self):
        result, lines, errors = self.execute(extra=["-s", "workspace-write", "--skip-git-repo-check"])
        self.assert_success(result, lines)
        self.assertEqual(errors, [])
        self.assert_call(True)
        self.assert_no_trust()

    def test_missing_codex_home_never_falls_back_to_home(self):
        self.repository()
        result, lines, errors = self.execute(extra=["-s", "workspace-write"],
                                             env={"CODEX_HOME": None})
        self.assert_success(result, lines)
        self.assertEqual(errors, ["orcexe-fake: CODEX_HOME not set, trust write skipped"])
        self.assertEqual(list(self.home.iterdir()), [])
        self.assert_no_trust()

    def test_explicit_read_only_and_danger_full_access_do_not_write_trust(self):
        self.repository()
        for sandbox in ("read-only", "danger-full-access"):
            with self.subTest(sandbox=sandbox):
                result, lines, errors = self.execute(extra=["-s", sandbox])
                self.assert_success(result, lines)
                self.assertEqual(errors, [])
                self.assert_no_trust()

    def test_write_creates_file_and_file_change_events(self):
        self.repository()
        result, lines, errors = self.execute("write", ["-s", "workspace-write", "-o", "final.txt"])
        self.assertEqual(result.returncode, 0)
        events = self.assert_types(lines, ["thread.started", "turn.started", "item.started",
                                           "item.completed", "item.completed", "turn.completed"])
        for event in events[2:4]:
            self.assertEqual(event["item"]["type"], "file_change")
            self.assertEqual(event["item"]["changes"], [{"path": "orcexe-fake-output.txt", "kind": "add"}])
        self.assertEqual(errors, [])
        self.assertEqual((self.work / "orcexe-fake-output.txt").read_bytes(), b"OK\n")
        self.assertEqual((self.work / "final.txt").read_bytes(), b"OK")
        self.assertTrue((self.codex_home / "config.toml").is_file())
        self.assert_call(True, "write")

    def test_synthetic_read_only_write_and_commit_refusal(self):
        self.repository()
        for scenario in ("write", "commit"):
            with self.subTest(scenario=scenario):
                result, lines, errors = self.execute(scenario, ["-o", "final.txt"])
                self.assertEqual(result.returncode, 1)
                self.assert_types(lines, ["thread.started", "turn.started", "error", "turn.failed"])
                self.assertEqual(errors, [])
                self.assertFalse((self.work / "orcexe-fake-output.txt").exists())
                self.assertFalse((self.work / "final.txt").exists())
                self.assert_call(True, scenario)
        self.assert_no_trust()

    def test_model_and_auth_failures_keep_errors_on_stdout(self):
        self.repository()
        for scenario, status in (("invalid-model", 400), ("auth-401", 401)):
            with self.subTest(scenario=scenario):
                result, lines, errors = self.execute(scenario, ["-s", "workspace-write", "-o", "absent.txt"])
                self.assertEqual(result.returncode, 1)
                events = self.assert_types(lines, ["thread.started", "item.completed", "turn.started",
                                                   "error", "turn.failed"])
                self.assertEqual(events[1]["item"]["type"], "error")
                body = json.loads(events[-2]["message"])
                self.assertEqual(body["status"], status)
                if status == 400:
                    self.assertEqual(body["error"]["code"], "model_not_found")
                else:
                    self.assertIn("Unauthorized", body["error"]["message"])
                self.assertEqual(events[-1]["error"]["message"], events[-2]["message"])
                self.assertEqual(errors, ["Reading additional input from stdin..."])
                self.assertFalse((self.work / "orcexe-fake-output.txt").exists())
                self.assertFalse((self.work / "absent.txt").exists())
                self.assert_call(True, scenario)
                self.assertTrue((self.codex_home / "config.toml").exists())

    def test_warning_before_turn_is_success(self):
        self.repository()
        result, lines, errors = self.execute("warning-first")
        self.assertEqual(result.returncode, 0)
        events = self.assert_types(lines, ["thread.started", "item.completed", "turn.started",
                                           "item.completed", "turn.completed"])
        self.assertEqual(events[1]["item"]["type"], "error")
        self.assertEqual(events[3]["item"]["text"], "OK")
        self.assertEqual(errors, [])
        self.assert_call(True, "warning-first")

    def test_synthetic_no_final_still_exits_zero(self):
        self.repository()
        result, lines, errors = self.execute("no-final", ["-o", "final.txt"])
        self.assertEqual(result.returncode, 0)
        self.assert_types(lines, ["thread.started", "turn.started", "item.completed"])
        self.assertEqual((self.work / "final.txt").read_bytes(), b"OK")
        self.assertEqual(errors, [])
        self.assert_call(True, "no-final")

    def test_synthetic_quota_failure(self):
        self.repository()
        result, lines, errors = self.execute("quota")
        self.assertEqual(result.returncode, 1)
        events = self.assert_types(lines, ["thread.started", "turn.started", "error", "turn.failed"])
        self.assertIn("usage limit", events[2]["message"])
        self.assertEqual(errors, [])
        self.assert_call(True, "quota")

    def test_synthetic_stall_timeout_after_flushed_events(self):
        self.repository()
        result, lines, errors = self.execute("stall", ready=True, timeout_s=scaled(.3))
        self.assertEqual(result.status, "timeout")
        self.assertGreaterEqual(result.duration_s, scaled(.3))
        self.assertTrue(result.tree_killed)
        if os.name != "nt":
            self.assertEqual(result.returncode, -signal.SIGTERM)
        self.assertLess(result.duration_s, scaled(1.5))
        self.assert_types(lines, ["thread.started", "turn.started"])
        self.assertEqual(errors, [])
        self.assert_call(True, "stall")

    def test_synthetic_commit_is_created_only_in_temp_repository(self):
        self.repository()
        result, lines, errors = self.execute("commit", ["-s", "workspace-write"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(self.events(lines)[-1]["type"], "turn.completed")
        self.assertEqual(errors, [])
        self.assertEqual(self.git_run("log", "-1", "--format=%s"), "fake")
        self.assertEqual(self.git_run("show", "HEAD:orcexe-fake-output.txt"), "OK")
        self.assertEqual(self.git_run("status", "--porcelain"), "")
        self.assert_call(True, "commit")

    def test_synthetic_big_line_is_bounded_and_followed_by_success(self):
        self.repository()
        result, lines, errors = self.execute("big-line", max_line_bytes=4096)
        self.assertEqual(result.truncated_lines, 1)
        self.assertGreater(result.stdout_bytes, 5 * 1024 * 1024)
        self.assertEqual(len(lines[0].encode()), 4096)
        self.assertIn('"type":"reasoning"', lines[0])
        self.assert_success(result, lines[1:])
        self.assertEqual(errors, [])
        self.assert_call(True, "big-line")

    def test_synthetic_background_child_holds_pipe(self):
        self.repository()
        result, lines, errors = self.execute("background-child", ready=True)
        self.assert_success(result, lines)
        self.assertLess(result.duration_s, scaled(2))
        # Windows job ownership includes the detached descendant and kills it.
        if os.name == "nt":
            self.assertTrue(result.tree_killed)
        else:
            self.assertTrue(result.pipes_held)
        self.assertEqual(errors, [])
        self.assert_call(True, "background-child")

    def rpc(self, requests, scenario="success", include_notifications=False):
        env = dict(self.env, ORCEXE_FAKE_SCENARIO=scenario)
        lines, errors = [], []
        argv = [sys.executable, "-B", "-c", RPC_CLIENT, json.dumps(self.command + ["app-server"]),
                json.dumps([json.dumps(request) if not isinstance(request, str) else request
                            for request in requests])]
        result = run(argv, cwd=str(self.work), env=env, timeout_s=scaled(4), stall_s=None,
                     on_line=lines.append, on_stderr_line=errors.append)
        self.assertEqual((result.status, result.returncode), ("exited", 0))
        self.assertEqual(errors, [])
        self.assert_call(False, scenario, ["app-server"])
        self.assert_no_trust()
        messages = self.events(lines)
        self.assertTrue(all("jsonrpc" not in message for message in messages))
        return messages if include_notifications else [m for m in messages if "id" in m]

    @staticmethod
    def request(method, request_id=1, **kwargs):
        return dict({"id": request_id, "method": method}, **kwargs)

    def initialize(self):
        return self.request("initialize", 0, params={"clientInfo": {"name": "orcexe-test"}})

    def test_appserver_round_trip_account_limits_and_initialize(self):
        messages = self.rpc([self.initialize(), {"method": "initialized"},
                             self.request("account/read", 1), self.request("account/rateLimits/read", 2)],
                            include_notifications=True)
        results = [m for m in messages if "id" in m]
        self.assertEqual([r["id"] for r in results], [0, 1, 2])
        self.assertEqual([m.get("method", "response") for m in messages],
                         ["remoteControl/status/changed", "account/updated", "response",
                          "account/updated", "response", "account/updated", "response"])
        for notification in (m for m in messages if "id" not in m):
            self.assertEqual(set(notification), {"method", "params", "emittedAtMs"})
            self.assertIs(type(notification["emittedAtMs"]), int)
        initialized = results[0]["result"]
        self.assertEqual(set(initialized), {"userAgent", "codexHome", "platformFamily", "platformOs"})
        self.assertEqual(initialized["codexHome"], str(self.codex_home))
        self.assertEqual(initialized["platformOs"], sys.platform)
        account = results[1]["result"]
        self.assertEqual(account, {"account": {"type": "chatgpt", "email": "fake-user@example.invalid", "planType": "plus"},
                                   "requiresOpenaiAuth": True,
                                   "workspaceRouting": {"accountId": "fake-acct", "workspaceId": "fake-workspace",
                                                        "backendOrigin": "https://example.invalid"}})
        limits = results[2]["result"]
        self.assertTrue(limits["ordinaryUsageAllowed"])
        self.assertEqual(set(limits), {"accountId", "ordinaryUsageAllowed", "rateLimitResetCredits",
                                      "rateLimitUpsell", "rateLimits", "rateLimitsByLimitId"})
        self.assertEqual(limits["accountId"], "fake-rate-limit-account")
        self.assertEqual(set(limits["rateLimits"]), {"credits", "individualLimit", "limitId", "limitName",
            "normalModelSlug", "planType", "primary", "rateLimitReachedType", "secondary", "spendControlReached"})
        self.assertEqual(limits["rateLimits"]["primary"], {"usedPercent": 23,
                         "windowDurationMins": 10080, "resetsAt": 1800000000})

    def test_synthetic_appserver_model_names(self):
        results = self.rpc([self.initialize(), {"jsonrpc": "2.0", "method": "initialized"},
                            self.request("model/list", "models", params={})])
        payload = results[1]["result"]
        self.assertEqual(results[1]["id"], "models")
        self.assertEqual(set(payload), {"data", "nextCursor"})
        self.assertIsNone(payload["nextCursor"])
        self.assertEqual(len(payload["data"]), 8)
        ids = {model["id"] for model in payload["data"]}
        self.assertTrue({"gpt-6.1-sol", "gpt-6.1-terra", "gpt-6-luna"}.issubset(ids))
        self.assertEqual(len(ids), 8)
        for model in payload["data"]:
            self.assertEqual(set(model), {"additionalSpeedTiers", "availabilityNux", "availableAccessPrograms",
                "defaultReasoningEffort", "defaultServiceTier", "description", "displayName", "hidden", "id",
                "inputModalities", "isDefault", "model", "modelSpecialty", "multiAgentVersion", "serviceTiers",
                "supportedReasoningEfforts", "supportsPersonality", "upgrade", "upgradeInfo"})
            self.assertIs(model["hidden"], False)

    def test_synthetic_appserver_quota_exhausted(self):
        results = self.rpc([self.initialize(), {"jsonrpc": "2.0", "method": "initialized"},
                            self.request("account/rateLimits/read")], "quota-exhausted")
        limits = results[1]["result"]
        self.assertFalse(limits["ordinaryUsageAllowed"])
        self.assertEqual(limits["rateLimits"]["primary"]["usedPercent"], 100)
        self.assertEqual(limits["rateLimits"]["rateLimitReachedType"], "primary")

    def test_appserver_scenario_is_independent_and_validated(self):
        for exec_scenario, rpc_scenario, allowed in (("quota", "quota-exhausted", False),
                                                     ("quota-exhausted", "normal", True)):
            with self.subTest(exec=exec_scenario, appserver=rpc_scenario):
                self.env["ORCEXE_FAKE_APPSERVER_SCENARIO"] = rpc_scenario
                results = self.rpc([self.initialize(), {"jsonrpc": "2.0", "method": "initialized"},
                                    self.request("account/rateLimits/read")], exec_scenario)
                self.assertIs(results[1]["result"]["ordinaryUsageAllowed"], allowed)
        self.env["ORCEXE_FAKE_APPSERVER_SCENARIO"] = "invalid"
        result, lines, errors = self.invoke(["app-server"])
        self.assertEqual(result.returncode, 3)
        self.assertEqual(lines, [])
        self.assertEqual(errors, ["orcexe-fake: unknown app-server scenario"])

    def test_synthetic_appserver_errors_and_recovery(self):
        results = self.rpc([self.request("account/read", 1), self.request("initialize", 2),
                            self.request("initialize", 3, params={"clientInfo": {"name": ""}}),
                            self.initialize(), {"jsonrpc": "2.0", "method": "initialized"},
                            self.request("unknown/method", 4), "{bad json", [],
                            self.request("account/read", 5)])
        self.assertEqual([r["id"] for r in results], [1, 2, 3, 0, 4, None, None, 5])
        self.assertEqual([r["error"]["code"] for r in results if "error" in r],
                         [-32002, -32602, -32602, -32601, -32700, -32600])
        self.assertEqual(results[0]["error"]["message"], "not initialized")
        self.assertIn("result", results[-1])

    def test_appserver_eof_is_clean_and_not_a_model_call(self):
        result, lines, errors = self.invoke(["app-server"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, [])
        self.assertEqual(errors, [])
        self.assert_call(False, argv=["app-server"])
        self.assert_no_trust()

    def test_synthetic_call_log_append_is_atomic_under_concurrency(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.invoke(["--version"]), range(8)))
        self.assertTrue(all(r[0].returncode == 0 for r in results))
        self.assertEqual(len(self.log()), 8)
        for index in range(8):
            self.assert_call(False, argv=["--version"], index=index)

    def test_fixture_json_provenance_and_python39_syntax(self):
        streams = sorted((FAKE / "streams").glob("codex-*.jsonl"))
        self.assertEqual(len(streams), 12)
        for path in streams:
            with self.subTest(path=path.name):
                lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
                header = lines[0]["_fake"]
                self.assertEqual(header["scenario"], path.stem[len("codex-"):])
                self.assertIn(header["source"], {"measured", "synthetic"})
                self.assertTrue(header["evidence"].startswith("T10 §"))
                self.assertTrue(all("type" in line and "_fake" not in line for line in lines[1:]))
        for path in (FAKE / "appserver").glob("*.json"):
            fixture = json.loads(path.read_text(encoding="utf-8"))
            self.assertIn("payload", fixture)
            self.assertEqual(fixture["_fake"]["source"], "measured 2026-11-16 (codex 0.163.0)")
        for path in (FAKE / "codex", FAKE / "_fake_common.py", Path(__file__)):
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path), feature_version=(3, 9))
        self.assertTrue((FAKE / "codex").read_bytes().startswith(b"#!/usr/bin/env python3\n"))
        self.assertNotIn(b"\r", (FAKE / "codex").read_bytes())
        self.assertTrue((FAKE / "codex.cmd").is_file())
        if os.name != "nt":
            self.assertTrue(os.access(FAKE / "codex", os.X_OK))

    @unittest.skipUnless(os.name == "nt", "Windows shim execution")
    def test_windows_shim_uses_current_python(self):
        self.assertEqual(self.command, ["codex.cmd"])
        result, lines, errors = self.invoke(["--version"])
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["codex-cli 0.163.0 (orcexe-fake)"])
        self.assertEqual(errors, [])
