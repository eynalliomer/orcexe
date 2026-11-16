"""Codex execution and probes against isolated fake homes and binaries."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import sys
import threading
from unittest import mock

from tests.unit._timing import run_when_ready, scaled
from tests.unit.test_gitutil import GitFixture, ROOT

from orcexe import proc
from orcexe.adapters import CodexAdapter, RunRequest, validate_run_result
from orcexe.adapters.codex_appserver import AppServerError


FAKE = ROOT / "tests" / "fixtures" / "fake-cli"
EXTRAS = ("ORCEXE_FAKE_SCENARIO", "ORCEXE_FAKE_APPSERVER_SCENARIO", "ORCEXE_FAKE_CALLS",
          "ORCEXE_FAKE_PYTHON", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL",
          "GIT_CONFIG_SYSTEM", "GIT_TERMINAL_PROMPT")


class CodexAdapterTests(GitFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.work = self.linked()
        self.calls = self.directory / "calls.jsonl"
        self.brief = self.directory / "brief-source.md"
        self.brief.write_text("Do the test task.\n", encoding="utf-8")
        self.env.update(ORCEXE_BIN_CODEX=str(FAKE / ("codex.cmd" if os.name == "nt" else "codex")),
                        ORCEXE_FAKE_CALLS=str(self.calls), ORCEXE_FAKE_PYTHON=sys.executable)
        self.adapter = self.make_adapter()

    def make_adapter(self, scenario="success", **options):
        return CodexAdapter(env=dict(self.env, ORCEXE_FAKE_SCENARIO=scenario), allow_extra=EXTRAS,
                            detect_timeout_s=scaled(3), appserver_timeout_s=scaled(5),
                            kill_grace_s=scaled(.1), **options)

    def request(self, name="test", profile="review", workdir=None):
        return RunRequest(name, "T13", "codex", "gpt-6-luna", "low", profile,
                          str(workdir or self.work), str(self.brief),
                          str(self.main / ".orcexe" / "runs" / name),
                          timeout_s=scaled(5), stall_s=scaled(4))

    def call_log(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def assert_model_free(self):
        self.assertEqual(sum(call["model_call"] for call in self.call_log()), 0)

    def test_auth_states_plan_and_account_privacy(self):
        for scenario, state in (("success", "ok"), ("auth-missing", "missing")):
            with self.subTest(scenario=scenario):
                result = self.make_adapter(scenario).auth_status()
                self.assertEqual(result, {"state": state, "method": "chatgpt" if state == "ok" else "unknown",
                                          "plan": "plus", "source": "probe"})
                encoded = json.dumps(result)
                for forbidden in ("fake-user@example.invalid", "fake-acct", "email", "workspaceRouting", "accountId"):
                    self.assertNotIn(forbidden, encoded)
        self.assert_model_free()
        missing = self.make_adapter()
        missing._env["ORCEXE_BIN_CODEX"] = str(self.directory / "absent")
        self.assertEqual(missing.auth_status(),
                         {"state": "unknown", "method": "unknown", "plan": None, "source": "probe"})

    def test_auth_stalled_fake_unknown_after_readiness(self):
        marker = self.directory / "ready"
        command = [sys.executable, "-B", "-c",
                   "import pathlib,threading; pathlib.Path({!r}).touch(); threading.Event().wait()".format(str(marker))]
        runner = proc.run

        def stalled(argv, **options):
            self.assertEqual(argv[1:], ["login", "status"])
            return run_when_ready(runner, command, marker.exists, self.addCleanup, **options)

        adapter = self.make_adapter(auth_timeout_s=scaled(.2))
        with mock.patch("orcexe.proc.run", side_effect=stalled), \
                mock.patch.object(adapter, "_session", side_effect=AppServerError(None, "offline")):
            self.assertEqual(adapter.auth_status(),
                             {"state": "unknown", "method": "unknown", "plan": None, "source": "probe"})

    def test_auth_unexpected_exit_api_key_and_probe_failure(self):
        for stream, code, expected in ((stream, code, state)
                for stream in ("on_line", "on_stderr_line")
                for code, state in ((0, "ok"), (1, "missing"), (2, "unknown"))):
            def status(argv, **options):
                options[stream]("Logged in using API key")
                return proc.ProcResult("exited", code, 0, 1, 0, 0, "", False, None)

            with mock.patch("orcexe.proc.run", side_effect=status), \
                    mock.patch.object(self.adapter, "_session", side_effect=AppServerError(-1, "fake-acct")):
                result = self.adapter.auth_status()
            self.assertEqual(result, {"state": expected, "method": "api_key", "plan": None, "source": "probe"})

    def test_models_and_normal_exhausted_quota_without_model_calls(self):
        models = self.adapter.list_models()
        self.assertEqual(models["source"], "app-server")
        self.assertEqual(len(models["models"]), 8)
        self.assertIn({"id": "gpt-6-luna", "supported_efforts": ["low", "medium", "high"],
                       "default_effort": "medium"}, models["models"])
        for model in models["models"]:
            self.assertEqual(set(model), {"id", "supported_efforts", "default_effort"})
        self.assertEqual(self.adapter.quota(), {
            "windows": [{"name": "primary", "used_pct": 23, "window_mins": 10080, "resets_at": 1800000000}],
            "allowed": True, "reached": None, "source": "app-server", "age_s": 0})
        self.adapter._env["ORCEXE_FAKE_APPSERVER_SCENARIO"] = "quota-exhausted"
        exhausted = self.adapter.quota()
        self.assertIs(exhausted["allowed"], False)
        self.assertEqual(exhausted["windows"][0]["used_pct"], 100)
        self.assertEqual(exhausted["reached"], "primary")
        self.assert_model_free()
        self.assertEqual([call["argv"] for call in self.call_log()], [["app-server"]] * 3)

    def test_all_appserver_probe_returns_exclude_identifying_fixture_values(self):
        account = json.loads((FAKE / "appserver" / "account.json").read_text())["payload"]
        limits = json.loads((FAKE / "appserver" / "rate-limits.json").read_text())["payload"]
        forbidden = (account["account"]["email"], account["workspaceRouting"]["accountId"],
                     account["workspaceRouting"]["workspaceId"], limits["accountId"],
                     "accountId", "email", "workspaceId", "workspaceRouting")
        results = []
        for scenario in ("normal", "quota-exhausted"):
            self.adapter._env["ORCEXE_FAKE_APPSERVER_SCENARIO"] = scenario
            results.extend((self.adapter.auth_status(), self.adapter.list_models(), self.adapter.quota()))
        self.assertTrue(all(result["source"] in ("probe", "app-server") for result in results))
        self.assertTrue(all(result["plan"] == "plus" for result in results if "plan" in result))
        self.assertTrue(all(result["models"] for result in results if "models" in result))
        for result in results:
            encoded = json.dumps(result, sort_keys=True)
            for value in forbidden:
                self.assertNotIn(value, encoded)
        self.assert_model_free()

    def test_quota_only_accesses_required_keys(self):
        class RequiredKeys(dict):
            def __getitem__(inner, key):
                if key not in inner:
                    raise AssertionError("accessed an unnecessary quota key")
                return super().__getitem__(key)

            def get(inner, key, *args):
                return inner[key]

        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.call.return_value = RequiredKeys(ordinaryUsageAllowed=True, rateLimits=RequiredKeys(
            rateLimitReachedType=None, secondary=None,
            primary=RequiredKeys(usedPercent=23, windowDurationMins=10080, resetsAt=1800000000)))
        with mock.patch.object(self.adapter, "_session", return_value=session):
            self.assertEqual(self.adapter.quota()["source"], "app-server")

    def test_probe_failures_are_short_and_do_not_expose_server_messages(self):
        with mock.patch.object(self.adapter, "_session", side_effect=AppServerError(123, "fake-acct private")):
            self.assertEqual(self.adapter.list_models(), {"models": None, "source": "none", "error": "app-server error"})
            self.assertEqual(self.adapter.quota(), {"windows": [], "allowed": None, "source": "none", "error": "app-server error"})
        self.assert_model_free()

    def test_model_pagination_uses_one_session_and_caps_at_five(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.call.side_effect = [{"data": [{"id": "a"}], "nextCursor": "page2"},
                                    {"data": [{"id": "b"}], "nextCursor": None}]
        with mock.patch.object(self.adapter, "_session", return_value=session) as create:
            self.assertEqual(self.adapter.list_models(), {"models": [{"id": "a"}, {"id": "b"}], "source": "app-server"})
        create.assert_called_once()
        self.assertEqual(session.call.call_args_list, [mock.call("model/list", {}),
                                                       mock.call("model/list", {"cursor": "page2"})])
        session.call.reset_mock(side_effect=True)
        session.call.return_value = {"data": [{"id": "a"}], "nextCursor": "again"}
        with mock.patch.object(self.adapter, "_session", return_value=session):
            self.assertIsNone(self.adapter.list_models()["models"])
        self.assertEqual(session.call.call_count, 5)

    def test_model_data_precedence_legacy_fallback_and_defensive_effort_names(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        model = {"id": "fake-model", "supportedReasoningEfforts": ["low",
            {"reasoningEffort": "medium", "description": "fake-user@example.invalid"},
            {"effort": "high"}, {"name": "xhigh"}, {"reasoningEffort": None, "effort": "max"},
            {}, {"effort": 7}, None, 42, []], "defaultReasoningEffort": "medium",
            "workspaceId": "fake-workspace", "displayName": "fake-acct"}
        expected = {"models": [{"id": "fake-model", "supported_efforts":
            ["low", "medium", "high", "xhigh", "max"], "default_effort": "medium"}], "source": "app-server"}
        with mock.patch.object(self.adapter, "_session", return_value=session):
            for key in ("data", "models"):
                session.call.return_value = {key: [model], "nextCursor": None}
                self.assertEqual(self.adapter.list_models(), expected)
            session.call.return_value = {"data": [], "models": [model], "nextCursor": None}
            self.assertEqual(self.adapter.list_models(), {"models": [], "source": "app-server"})
            session.call.return_value = {"data": None, "models": [model], "nextCursor": None}
            self.assertEqual(self.adapter.list_models()["source"], "none")
            for efforts in (None, {}, "low", 7):
                session.call.return_value = {"data": [{"id": "a", "supportedReasoningEfforts": efforts,
                                                       "defaultReasoningEffort": {}}], "nextCursor": None}
                self.assertEqual(self.adapter.list_models()["models"],
                                 [{"id": "a", "supported_efforts": [], "default_effort": None}])

    def test_only_account_plan_type_is_used_and_invalid_shapes_fail_honestly(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        with mock.patch.object(self.adapter, "_session", return_value=session):
            for account in ({"planType": "plus"}, {"planType": 123}, None):
                session.call.return_value = {"account": account, "planType": "wrong"}
                self.assertEqual(self.adapter.auth_status()["plan"], "plus" if account == {"planType": "plus"} else None)
            for payload in (None, [], {}, {"data": [{"id": 3}], "nextCursor": None}):
                session.call.return_value = payload
                self.assertEqual(self.adapter.list_models()["source"], "none")
                self.assertEqual(self.adapter.quota()["source"], "none")

    def test_secondary_quota_window_and_probe_serialization(self):
        session = mock.MagicMock()
        session.__enter__.return_value = session
        session.call.return_value = {"ordinaryUsageAllowed": True, "rateLimits": {
            "primary": {"usedPercent": 10, "windowDurationMins": 60, "resetsAt": 123},
            "secondary": {"usedPercent": 20, "windowDurationMins": 120, "resetsAt": 456},
            "rateLimitReachedType": None}}
        entered, release = threading.Event(), threading.Event()
        count_lock = threading.Lock()
        active, maximum = [0], [0]

        def enter():
            with count_lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            entered.set()
            if not release.wait(scaled(5)):
                raise AssertionError("probe release missing")
            return session

        def leave(*args):
            with count_lock:
                active[0] -= 1

        session.__enter__.side_effect = enter
        session.__exit__.side_effect = leave
        with mock.patch.object(self.adapter, "_session", return_value=session):
            with ThreadPoolExecutor(max_workers=3) as pool:
                calls = [pool.submit(self.adapter.quota) for _ in range(3)]
                try:
                    self.assertTrue(entered.wait(scaled(5)))
                finally:
                    release.set()
                results = [pending.result(timeout=scaled(5)) for pending in calls]
        self.assertEqual(maximum[0], 1)
        self.assertEqual([w["name"] for w in results[0]["windows"]], ["primary", "secondary"])

    def test_final_file_preferred_stream_fallback_and_last_usage(self):
        req = self.request("file")
        runner = proc.run

        def execution(argv, **options):
            result = runner(argv, **options)
            if argv[1] == "exec":
                Path(argv[argv.index("-o") + 1]).write_text("authoritative final", encoding="utf-8")
            return result

        with mock.patch("orcexe.proc.run", side_effect=execution):
            result = self.adapter.run(req)
        self.assertEqual(Path(result["final_text_path"]).read_text(), "authoritative final")
        self.assertNotIn("final_text_from_stream", result["warnings"])

        def no_output_file(argv, **options):
            result = runner(argv, **options)
            if argv[1] == "exec":
                Path(argv[argv.index("-o") + 1]).write_text("", encoding="utf-8")
            return result

        with mock.patch("orcexe.proc.run", side_effect=no_output_file):
            result = self.adapter.run(self.request("fallback"))
        self.assertEqual(Path(result["final_text_path"]).read_text(), "OK")
        self.assertEqual(result["warnings"], ["final_text_from_stream"])
        events = [{"type": "turn.completed", "usage": {"input_tokens": 1}},
                  {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 2,
                   "cache_write_input_tokens": 3, "output_tokens": 4, "reasoning_output_tokens": 5}}]
        extracted = self.adapter._extract(events)
        self.assertEqual(extracted["usage"], {"input": 10, "cached_read": 2, "cache_write": 3, "output": 4, "reasoning": 5})
        self.assertIsNone(extracted["model_actual"])
        self.assertTrue(all(value is None for value in self.adapter._extract([])["usage"].values()))
        fallback = self.adapter._extract([{"type": "item.completed", "item": {
            "type": "agent_message", "text": "first"}}, {"type": "item.completed", "item": {
            "type": "agent_message", "text": "last"}}])
        self.assertEqual((fallback["final_text"], fallback["warnings"]), ("last", ["final_text_from_stream"]))

    def test_classification_priority_errors_redaction_and_quota_confirmation(self):
        exited = proc.ProcResult("exited", 1, 0, 0, 0, 0, "", False, None)

        def classify(message, code=1, counts=None):
            summary = {"events": [{"type": "error", "message": message}],
                       "counts": counts or {"turn.failed": 1}}
            outcome = self.adapter._classify(summary, replace(exited, returncode=code))
            return outcome, summary

        for message in ("HTTP 401", "Unauthorized", "unauthorized",
                        {"message": json.dumps({"status": 401, "error": "access denied"})},
                        json.dumps({"status": 401, "error": {"message": "access denied"}})):
            self.assertEqual(classify(message)[0], ("auth", []))
        self.assertEqual(classify('{"error":{"code":"model_not_found"}}')[0], ("failed", ["model_not_found"]))
        for quota in ({"windows": [], "allowed": False},
                      {"windows": [{"used_pct": 100}], "allowed": True},
                      {"windows": [], "allowed": True, "reached": "primary"}):
            with mock.patch.object(self.adapter, "quota", return_value=quota) as probe:
                self.assertEqual(classify("RATE LIMIT exhausted")[0], ("quota", []))
                probe.assert_called_once()
        for quota in ({"windows": [], "allowed": True, "reached": None}, {"windows": [], "allowed": None}):
            with mock.patch.object(self.adapter, "quota", return_value=quota):
                self.assertEqual(classify("usage limit")[0], ("failed", ["quota_suspected"]))
        self.assertEqual(classify("warning", 0, {"turn.completed": 1})[0], ("completed", []))
        self.assertEqual(classify("", 0, {"turn.started": 1})[0], ("failed", ["final_event_missing"]))
        outcome, summary = classify("fake-user@example.invalid " + "x" * 1000)
        self.assertEqual(outcome, ("failed", ["unknown_failure"]))
        self.assertLessEqual(len(summary["last_error"]), 500)
        self.assertNotIn("fake-user@example.invalid", summary["last_error"])

    def test_quota_unavailable_never_success_and_confirmed_after_execution(self):
        adapter = self.make_adapter("quota")
        result = adapter.run(self.request("unconfirmed"))
        self.assertEqual((result["status"], result["warnings"]), ("failed", ["quota_suspected"]))
        with mock.patch.object(adapter, "quota", return_value={"windows": [], "allowed": None}):
            result = adapter.run(self.request("unavailable"))
        self.assertEqual((result["status"], result["warnings"]), ("failed", ["quota_suspected"]))
        self.calls.unlink()
        adapter._env["ORCEXE_FAKE_APPSERVER_SCENARIO"] = "quota-exhausted"
        result = adapter.run(self.request("confirmed"))
        self.assertEqual((result["status"], result["warnings"]), ("quota", []))
        self.assertEqual([call["argv"][0] for call in self.call_log()], ["--version", "exec", "app-server"])
        self.assertEqual(sum(call["model_call"] for call in self.call_log()), 1)
        validate_run_result(result)

    def test_trust_header_hash_only_for_write_in_git(self):
        home = Path(self.env["CODEX_HOME"])
        home.mkdir()
        config = home / "config.toml"
        initial = '[projects."existing"]\ntrust_level = "trusted"\nprivate_value = "never retain"\n'
        config.write_text(initial, encoding="utf-8")
        self.assertEqual(self.adapter._trust_headers(), {'[projects."existing"]'})
        self.assertEqual(self.adapter.run(self.request("review"))["warnings"], [])
        self.assertEqual(config.read_text(), initial)
        req = self.request("write", "write")
        result = self.adapter.run(req)
        header = "[projects.{}]".format(json.dumps(str(self.work.resolve()), ensure_ascii=False))
        warning = "trust_entry_added:" + hashlib.sha256(header.encode()).hexdigest()[:12]
        self.assertEqual(result["warnings"], ["empty_diff", warning])
        self.assertEqual(self.adapter.run(self.request("write-again", "write"))["warnings"], ["empty_diff"])
        self.assertIn(initial, config.read_text())
        self.assertEqual(json.loads((Path(req.run_dir) / "result.json").read_text()), result)
        with mock.patch("orcexe.adapters.base.Adapter.run", return_value={"warnings": []}), \
                mock.patch.object(self.adapter, "_trust_headers") as scan:
            self.assertEqual(self.adapter.run(self.request("non-git", "write", self.home)), {"warnings": []})
            scan.assert_not_called()
        self.assertFalse((self.home / ".codex").exists())

    def test_explicit_env_and_home_fallback_are_isolated(self):
        self.adapter._env.update(OPENAI_API_KEY="secret", CODEX_API_KEY="secret", EXTRA="drop")
        self.assertNotIn("OPENAI_API_KEY", self.adapter._environment())
        self.assertNotIn("CODEX_API_KEY", self.adapter._environment())
        self.assertNotIn("EXTRA", self.adapter._environment())
        self.assertEqual(self.adapter._environment()["CODEX_HOME"], self.env["CODEX_HOME"])
        del self.adapter._env["CODEX_HOME"]
        config = self.home / ".codex" / "config.toml"
        config.parent.mkdir()
        config.write_text('[projects."temp-home"]\nother = "discard"\n')
        self.assertEqual(self.adapter._trust_headers(), {'[projects."temp-home"]'})
