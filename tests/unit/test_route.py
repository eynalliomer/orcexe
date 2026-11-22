"""Prior routing and model-free fake Codex access; no real homes or AI CLIs."""

from contextlib import ExitStack
import copy
from dataclasses import asdict, FrozenInstanceError, replace
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

from tests.unit._timing import scaled


ROOT = Path(__file__).resolve().parents[2]
PLUGIN = ROOT / "plugins/orcexe"
FAKE = ROOT / "tests/fixtures/fake-cli"
sys.path.insert(0, str(PLUGIN / "src"))

from orcexe.adapters import CodexAdapter, ClaudeCodeAdapter
from orcexe.manifest import load_manifest
from orcexe.miniyaml import load_path
from orcexe.route import Access, gather_access, load_roles, route, validate_route


FIRST = "gpt-6.1-sol"
SECOND = "gpt-6-sol"
EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
SEEN = Access("codex", "openai", True, "ok", {FIRST: EFFORTS, SECOND: EFFORTS}, True, 61.0)


class RolesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="route-roles-")
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "roles.yaml"
        self.raw = load_path(PLUGIN / "data/experience/roles.yaml")

    def load_data(self, data):
        # JSON is in miniyaml's flow subset; mutations still exercise load_roles.
        self.path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return load_roles(self.path)

    def test_shipped_pack_and_immutable_priors(self):
        roles = load_roles()
        self.assertEqual(roles.version, 1)
        self.assertEqual(roles.as_of, "2026-11-22")
        self.assertEqual(roles.evidence,
                         "VARSAYIM — karar 104; v0.6 değerlendirme programında ölçülecek")
        expected = {
            "manager": {"any": [("claude-opus-5-5", "high")]},
            "executor": {"fast": [(FIRST, "medium"), (SECOND, "medium"), ("claude-sonnet-5-5", "medium"), ("composer-2.5", "medium")],
                         "standard": [(FIRST, "high"), (SECOND, "high"), ("claude-sonnet-5-5", "high"), ("composer-2.5", "medium")], "risky": [(FIRST, "high"), ("claude-opus-5-5", "high"), ("composer-2.5", "medium")]},
            "evaluator": {"any": [("claude-sonnet-5-5", "medium"), (FIRST, "medium"), ("claude-opus-5-5", "medium"), ("gpt-6-sol", "medium")]}, "auditor": {"any": [("claude-opus-5-5", "high"), (FIRST, "high")]}}
        self.assertEqual({stage: {lane: [(c.model, c.effort) for c in candidates]
                                 for lane, candidates in lanes.items()}
                          for stage, lanes in roles.stages.items()}, expected)
        self.assertEqual({c.tool for lanes in roles.stages.values() for candidates in lanes.values()
                          for c in candidates}, {"codex", "claude-code", "cursor"})
        with self.assertRaises(FrozenInstanceError):
            roles.as_of = "2026-01-01"
        with self.assertRaises(TypeError):
            roles.stages["executor"]["fast"] = ()
        with self.assertRaises(FrozenInstanceError):
            roles.stages["executor"]["fast"][0].effort = "low"

    def test_invalid_schema_mutation_table_names_key(self):
        cases = [
            ((), None, "roles"), ((), [], "roles"),
            (("version",), True, "version"), (("version",), 2, "version"),
            (("version",), "1", "version"), (("version",), 1.0, "version"),
            (("as_of",), 20261117, "as_of"), (("as_of",), "2026-02-30", "as_of"),
            (("as_of",), "20261117", "as_of"), (("evidence",), "", "evidence"),
            (("evidence",), [], "evidence"), (("stages",), [], "stages"),
            (("stages",), {}, "stages"), (("stages", "writer"), {}, "writer"),
            (("stages", "executor"), [], "executor"), (("stages", "executor"), {}, "executor"),
            (("stages", "executor", "slow"), [], "slow"),
            (("stages", "executor", "fast"), [], "fast"),
            (("stages", "executor", "fast"), {}, "fast"),
            (("stages", "executor", "fast"), None, "fast"),
            (("stages", "executor", "fast", 0), [], "fast[0]"),
            (("stages", "executor", "fast", 0, "tool"), None, "tool"),
            (("stages", "executor", "fast", 0, "model"), 42, "model"),
            (("stages", "executor", "fast", 0, "model"), "", "model"),
            (("stages", "executor", "fast", 0, "effort"), "extreme", "effort"),
            (("stages", "executor", "fast", 0, "effort"), ["low"], "effort"),
        ]
        for path, value, key in cases:
            with self.subTest(path=path, value=value):
                data = copy.deepcopy(self.raw)
                if path:
                    target = data
                    for part in path[:-1]:
                        target = target[part]
                    target[path[-1]] = value
                else:
                    data = value
                with self.assertRaises(ValueError) as caught:
                    self.load_data(data)
                self.assertIn(key, str(caught.exception))

    def test_missing_and_unknown_keys_at_every_schema_mapping(self):
        paths = [(), ("stages",), ("stages", "executor"), ("stages", "executor", "fast", 0)]
        for path in paths:
            keys = {(): ("version", "as_of", "evidence", "stages"),
                    ("stages", "executor", "fast", 0): ("tool", "model", "effort")}.get(path, ())
            for name in ("unexpected",) + keys:
                with self.subTest(path=path, key=name):
                    data = copy.deepcopy(self.raw)
                    target = data
                    for part in path:
                        target = target[part]
                    if name == "unexpected":
                        target[name] = True
                    else:
                        del target[name]
                    with self.assertRaises(ValueError) as caught:
                        self.load_data(data)
                    self.assertIn(name, str(caught.exception))

    def test_invalid_yaml_names_root_or_offending_key(self):
        for text, key in (("version: 1\nversion: 1\n", "key"),
                          ("stages: [unterminated", "<root>"),
                          ("stages: &prior {}", "<root>")):
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                with self.assertRaises(ValueError) as caught:
                    load_roles(self.path)
                self.assertIn(key, str(caught.exception))

    def test_default_path_is_package_relative(self):
        with mock.patch("pathlib.Path.cwd", side_effect=AssertionError("must not use cwd")):
            self.assertEqual(load_roles().as_of, "2026-11-22")


class RouteTests(unittest.TestCase):
    def setUp(self):
        self.roles = load_roles()

    def choose(self, access=SEEN, *, constraints=None, stage="executor", lane="fast"):
        result = route(stage, lane, [] if access is None else [access], self.roles, constraints=constraints)
        validate_route(result)
        return result

    def test_first_available_exact_result(self):
        self.assertEqual(self.choose(), {
            "schema": "orcexe.route.v1", "stage": "executor", "lane": "fast", "tool": "codex",
            "family": "openai", "model": FIRST, "effort": "medium", "confidence": "normal",
            "basis": ["prior:roles.yaml#executor.fast[0]", "access:live", "quota:ok(61%)"],
            "skipped": [], "roles_as_of": "2026-11-22"})

    def test_live_model_absent_selects_second_prior(self):
        result = self.choose(replace(SEEN, models={SECOND: EFFORTS}))
        self.assertEqual(result["model"], SECOND)
        self.assertEqual(result["confidence"], "normal")
        self.assertEqual(result["skipped"], [{"candidate": "codex/" + FIRST, "reason": "model_unavailable"}])
        self.assertIn("prior:roles.yaml#executor.fast[1]", result["basis"])

    def test_rejection_table_and_no_substitutes(self):
        cases = [
            (None, {}, "tool_absent"), (replace(SEEN, present=False), {}, "tool_absent"),
            (replace(SEEN, auth="missing"), {}, "auth_missing"),
            (replace(SEEN, auth="unknown"), {}, "auth_unknown"),
            (replace(SEEN, quota_allowed=False), {}, "quota_exhausted"),
            (replace(SEEN, quota_used_pct=97), {}, "quota_exhausted"),
            (replace(SEEN, quota_used_pct=95), {}, "quota_exhausted"),
            (SEEN, {"exclude_families": ["openai"]}, "family_excluded"),
            (SEEN, {"deny_models": [FIRST, SECOND]}, "model_denied"),
            (SEEN, {"tools": []}, "tool_not_allowed"),
            (SEEN, {"tools": ["other"]}, "tool_not_allowed"),
            (replace(SEEN, models={}), {}, "model_unavailable"),
            (replace(SEEN, tool="other"), {}, "tool_absent"),
            (replace(SEEN, models={FIRST + "-extra": EFFORTS}), {}, "model_unavailable"),
        ]
        for observed, constraints, reason in cases:
            with self.subTest(access=observed, constraints=constraints):
                result = self.choose(observed, constraints=constraints)
                self.assertIsNone(result["tool"])
                self.assertEqual(result["reason"], "no_eligible_candidate")
                self.assertEqual(result["skipped"], [
                    {"candidate": "codex/" + name, "reason": reason} for name in (FIRST, SECOND)] +
                    [{"candidate": "claude-code/claude-sonnet-5-5", "reason": "tool_absent"},
                     {"candidate": "cursor/composer-2.5", "reason": "tool_absent"}])
                for name in (FIRST, SECOND):
                    self.assertIn("skip:codex/" + name + ":" + reason, result["basis"])

    def test_filters_have_specified_precedence(self):
        observed = replace(SEEN, present=False, auth="missing", models={}, quota_allowed=False)
        constraints = {"exclude_families": ["openai"], "deny_models": [FIRST, SECOND], "tools": []}
        steps = [(observed, "tool_absent"), (replace(observed, present=True), "auth_missing"),
                 (replace(observed, present=True, auth="unknown"), "auth_unknown"),
                 (replace(observed, present=True, auth="ok"), "family_excluded")]
        for access, reason in steps:
            self.assertEqual(self.choose(access, constraints=constraints)["skipped"][0]["reason"], reason)
        observed = replace(observed, present=True, auth="ok")
        del constraints["exclude_families"]
        self.assertEqual(self.choose(observed, constraints=constraints)["skipped"][0]["reason"], "model_denied")
        del constraints["deny_models"]
        self.assertEqual(self.choose(observed, constraints=constraints)["skipped"][0]["reason"], "model_unavailable")
        observed = replace(observed, models=SEEN.models)
        self.assertEqual(self.choose(observed, constraints=constraints)["skipped"][0]["reason"], "quota_exhausted")
        observed = replace(observed, quota_allowed=True)
        self.assertEqual(self.choose(observed, constraints=constraints)["skipped"][0]["reason"], "tool_not_allowed")

    def test_constraints_can_skip_first_without_excluding_second(self):
        result = self.choose(constraints={"tools": ["codex"], "deny_models": [FIRST],
                                          "exclude_families": ["unrelated"]})
        self.assertEqual(result["model"], SECOND)
        self.assertEqual(result["skipped"][0]["reason"], "model_denied")

    def test_quota_below_threshold_and_unknown_quota(self):
        self.assertEqual(self.choose(replace(SEEN, quota_used_pct=94.99))["tool"], "codex")
        result = self.choose(replace(SEEN, quota_allowed=None, quota_used_pct=None))
        self.assertEqual(result["confidence"], "normal")
        self.assertIn("quota:not_seen", result["basis"])

    def test_live_list_not_seen_uses_prior_with_low_confidence(self):
        result = self.choose(replace(SEEN, models=None))
        self.assertEqual(result["model"], FIRST)
        self.assertEqual(result["confidence"], "low")
        self.assertIn("access:not_seen", result["basis"])

    def test_force_available_even_outside_prior(self):
        forced = {"tool": "codex", "model": "live-only", "effort": "high"}
        result = self.choose(replace(SEEN, models={"live-only": EFFORTS}), constraints={"force": forced})
        self.assertEqual({k: result[k] for k in forced}, forced)
        self.assertIn("constraint:force", result["basis"])
        self.assertEqual(result["skipped"], [])

    def test_force_unavailable_never_substitutes_and_retains_every_filter(self):
        forced = {"tool": "codex", "model": FIRST, "effort": "medium"}
        cases = [(replace(SEEN, models={SECOND: EFFORTS}), {}, "model_unavailable"),
                 (None, {}, "tool_absent"), (replace(SEEN, auth="missing"), {}, "auth_missing"),
                 (replace(SEEN, quota_allowed=False), {}, "quota_exhausted"),
                 (SEEN, {"tools": []}, "tool_not_allowed"),
                 (SEEN, {"deny_models": [FIRST]}, "model_denied"),
                 (SEEN, {"exclude_families": ["openai"]}, "family_excluded")]
        for observed, extra, reason in cases:
            with self.subTest(reason=reason):
                result = self.choose(observed, constraints=dict(extra, force=forced))
                self.assertIsNone(result["tool"])
                self.assertEqual(result["reason"], "forced_unavailable")
                self.assertEqual(result["skipped"], [{"candidate": "codex/" + FIRST, "reason": reason}])
                self.assertIn("reason:forced_unavailable", result["basis"])

    def test_effort_adjustments_prefer_lower_even_if_higher_is_closer(self):
        cases = [("medium", ("high", "low"), "low"), ("medium", ("ultra", "high"), "high"),
                 ("high", ("low", "medium", "xhigh"), "medium"),
                 ("high", ("low", "xhigh"), "low"), ("medium", (), "medium"),
                 ("medium", EFFORTS, "medium")]
        for requested, supported, expected in cases:
            with self.subTest(requested=requested, supported=supported):
                forced = {"tool": "codex", "model": FIRST, "effort": requested}
                access = replace(SEEN, models={FIRST: supported})
                # Exercise both shipped priors and force's identical effort rule.
                choices = [self.choose(access, constraints={"force": forced})]
                if requested == "medium":
                    choices.append(self.choose(access))
                for result in choices:
                    self.assertEqual(result["effort"], expected)
                    adjustments = [s for s in result["basis"] if s.startswith("effort_adjusted:")]
                    self.assertEqual(adjustments, [] if requested == expected else
                                     ["effort_adjusted:{}->{}".format(requested, expected)])

    def test_nothing_eligible_retains_different_skip_reasons(self):
        result = self.choose(replace(SEEN, models={SECOND: EFFORTS}, quota_allowed=False))
        self.assertIsNone(result["tool"])
        self.assertEqual([s["reason"] for s in result["skipped"]], ["model_unavailable", "quota_exhausted", "tool_absent", "tool_absent"])

    def test_any_fallback_and_exact_lane_precedence(self):
        for stage, effort in (("evaluator", "medium"), ("auditor", "high")):
            for lane in ("fast", "standard", "risky", "any"):
                result = self.choose(stage=stage, lane=lane)
                self.assertEqual(result["effort"], effort)
                self.assertEqual(result["lane"], lane)
                self.assertIn("prior:roles.yaml#{}.any[1]".format(stage), result["basis"])
        candidate = self.roles.stages["executor"]["fast"]
        roles = replace(self.roles, stages={"executor": {"fast": candidate, "any": candidate[1:]}})
        result = route("executor", "fast", [replace(SEEN, models={})], roles)
        self.assertEqual(len(result["skipped"]), 4)
        self.assertNotIn("prior:roles.yaml#executor.any[0]", result["basis"])

    def test_cursor_executor_fallback_after_existing_candidates(self):
        cursor = Access("cursor", "cursor", True, "ok", {"composer-2.5": ()}, None, None)
        selected = route("executor", "fast", [cursor], self.roles)
        self.assertEqual((selected["tool"], selected["family"], selected["model"], selected["effort"]),
                         ("cursor", "cursor", "composer-2.5", "medium"))
        self.assertEqual(len(selected["skipped"]), 3)
        self.assertEqual(route("executor", "fast", [SEEN, cursor], self.roles)["tool"], "codex")

    def test_claude_fallback_and_counter_family_reviewers(self):
        claude = Access("claude-code", "anthropic", True, "ok", None, None, None)
        access = [replace(SEEN, auth="missing"), claude]
        result = route("executor", "fast", access, self.roles)
        self.assertEqual((result["tool"], result["model"], result["confidence"]),
                         ("claude-code", "claude-sonnet-5-5", "low"))
        self.assertIn("access:not_seen", result["basis"])
        for stage, model, effort in (("evaluator", "claude-sonnet-5-5", "medium"),
                                     ("auditor", "claude-opus-5-5", "high")):
            review = route(stage, "any", [SEEN, claude], self.roles)
            self.assertEqual((review["tool"], review["model"], review["effort"]),
                             ("claude-code", model, effort))

    def test_missing_prior_honest_failure(self):
        roles = replace(self.roles, stages={})
        self.assertEqual(route("executor", "fast", [SEEN], roles)["reason"], "no_prior")
        forced = {"tool": "codex", "model": FIRST, "effort": "high"}
        self.assertEqual(route("executor", "fast", [SEEN], roles, constraints={"force": forced})["model"], FIRST)

    def test_determinism_and_no_input_mutation(self):
        access = [SEEN, replace(SEEN, tool="unrelated")]
        constraints = {"deny_models": [FIRST]}
        before = copy.deepcopy((access, constraints))
        result = route("executor", "fast", access, self.roles, constraints=constraints)
        for _ in range(5):
            self.assertEqual(route("executor", "fast", list(reversed(access)), self.roles,
                                   constraints=constraints), result)
        self.assertEqual((access, constraints), before)

    def test_invalid_inputs_name_key(self):
        for key, value in (("unexpected", []), ("tools", "codex"), ("deny_models", [None]),
                           ("exclude_families", 1), ("force", {}), ("force", None)):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, key):
                self.choose(constraints={key: value})
        for stage, lane, key in (("writer", "fast", "stage"), ([], "fast", "stage"),
                                 ("executor", "slow", "lane")):
            with self.assertRaisesRegex(ValueError, key):
                self.choose(stage=stage, lane=lane)
        with self.assertRaisesRegex(ValueError, "access.codex"):
            route("executor", "fast", [SEEN, SEEN], self.roles)

    def test_validate_route_mutation_table(self):
        success, failure = self.choose(), self.choose(None)
        cases = [(success, "schema", "other"), (success, "stage", "writer"),
                 (success, "lane", "slow"), (success, "effort", "extreme"),
                 (success, "tool", ""), (success, "family", None), (success, "model", 12),
                 (success, "confidence", True), (success, "basis", []), (success, "basis", [1]),
                 (success, "skipped", {}), (success, "skipped", [{"candidate": "x", "reason": "other"}]),
                 (success, "skipped", [{"candidate": "x", "reason": "tool_absent", "extra": 1}]),
                 (success, "roles_as_of", "2026-02-30"), (success, "unexpected", True),
                 (success, "reason", "no_prior"), (failure, "reason", "other"),
                 (failure, "model", FIRST), (failure, "confidence", "normal")]
        for original, key, value in cases:
            with self.subTest(key=key, value=value), self.assertRaisesRegex(ValueError, key):
                data = copy.deepcopy(original)
                data[key] = value
                validate_route(data)
        for key in success:
            with self.subTest(missing=key), self.assertRaisesRegex(ValueError, key):
                data = copy.deepcopy(success)
                del data[key]
                validate_route(data)
        unseen = self.choose(replace(SEEN, models=None))
        unseen["confidence"] = "normal"
        with self.assertRaisesRegex(ValueError, "confidence"):
            validate_route(unseen)


class GatherAccessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="route-access-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.calls = self.directory / "calls.jsonl"
        self.manifest = load_manifest(PLUGIN / "adapters/codex.yaml")

    def stub(self):
        return SimpleNamespace(manifest=self.manifest,
            detect=mock.Mock(return_value={"present": True, "path": "/private/binary"}),
            auth_status=mock.Mock(return_value={"state": "ok", "email": "private@example.invalid"}),
            list_models=mock.Mock(return_value={"source": "app-server", "models": [
                {"id": FIRST, "supported_efforts": ["medium", "low", "unknown"], "accountId": "secret"}]}),
            quota=mock.Mock(return_value={"source": "app-server", "allowed": True,
                "windows": [{"used_pct": 23}, {"used_pct": 61}], "accountId": "secret"}))

    def assert_once(self, adapter):
        for method in ("detect", "auth_status", "list_models", "quota"):
            getattr(adapter, method).assert_called_once_with()

    def test_claude_probe_remains_model_free_and_access_not_seen(self):
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
               if key in os.environ}
        env.update(PATH=os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
            HOME=str(self.directory), CODEX_HOME=str(self.directory / "codex"),
            CLAUDE_CONFIG_DIR=str(self.directory / "claude"), ORCEXE_STATE_DIR=str(self.directory / "state"),
            ORCEXE_BIN_CLAUDE_CODE=str(FAKE / ("claude.cmd" if os.name == "nt" else "claude")),
            ORCEXE_FAKE_PYTHON=sys.executable, ORCEXE_FAKE_CALLS=str(self.calls))
        adapter = ClaudeCodeAdapter(env=env, allow_extra=("ORCEXE_FAKE_PYTHON", "ORCEXE_FAKE_CALLS"),
                                    detect_timeout_s=scaled(3), kill_grace_s=scaled(.1))
        observed, = gather_access([adapter])
        self.assertEqual(observed, Access("claude-code", "anthropic", True, "ok", None, None, None))
        decision = route("executor", "fast", [observed], load_roles())
        self.assertEqual(decision["tool"], "claude-code")
        self.assertEqual(decision["confidence"], "low")
        self.assertIn("access:not_seen", decision["basis"])
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["argv"] for call in calls], [["--version"], ["auth", "status", "--json"]])
        self.assertFalse(any(call["model_call"] for call in calls))

    def test_fake_normal_exhausted_and_missing_binary_are_model_free(self):
        home = self.directory / "home"
        home.mkdir()
        env = {key: os.environ[key] for key in ("SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
               if key in os.environ}
        env.update(PATH=os.pathsep.join((str(Path(sys.executable).parent), os.defpath)),
                   HOME=str(home), USERPROFILE=str(home), CODEX_HOME=str(self.directory / "codex-home"),
                   ORCEXE_BIN_CODEX=str(FAKE / ("codex.cmd" if os.name == "nt" else "codex")),
                   ORCEXE_FAKE_PYTHON=sys.executable, ORCEXE_FAKE_CALLS=str(self.calls),
                   PYTHONDONTWRITEBYTECODE="1", ORCEXE_FAKE_SCENARIO="success")
        results = []
        for scenario in ("normal", "quota-exhausted", "missing"):
            with self.subTest(scenario=scenario):
                current = dict(env, ORCEXE_FAKE_APPSERVER_SCENARIO=scenario)
                if scenario == "missing":
                    current["ORCEXE_BIN_CODEX"] = str(self.directory / "missing-binary")
                adapter = CodexAdapter(self.manifest, env=current,
                    allow_extra=("ORCEXE_FAKE_PYTHON", "ORCEXE_FAKE_CALLS", "ORCEXE_FAKE_SCENARIO",
                                 "ORCEXE_FAKE_APPSERVER_SCENARIO"),
                    detect_timeout_s=scaled(3), appserver_timeout_s=scaled(3), kill_grace_s=scaled(.1))
                with ExitStack() as stack:
                    for method in ("detect", "auth_status", "list_models", "quota"):
                        stack.enter_context(mock.patch.object(adapter, method, wraps=getattr(adapter, method)))
                    observed, = gather_access([adapter])
                    self.assert_once(adapter)
                results.append(observed)
                self.assertEqual((observed.tool, observed.family), ("codex", "openai"))
                if scenario == "missing":
                    self.assertEqual(observed, Access("codex", "openai", False, "unknown", None, None, None))
                    continue
                self.assertTrue(observed.present)
                self.assertEqual(observed.auth, "ok")
                self.assertEqual(observed.models[FIRST], ("low", "medium", "high"))
                self.assertEqual(len(observed.models), 8)
                self.assertEqual((observed.quota_allowed, observed.quota_used_pct),
                                 (True, 23) if scenario == "normal" else (False, 100))
                selected = route("executor", "fast", [observed], load_roles())
                self.assertEqual(selected["tool"], "codex" if scenario == "normal" else None)
        calls = [json.loads(line) for line in self.calls.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([c["argv"] for c in calls],
                         [["--version"], ["login", "status"], ["app-server"], ["app-server"], ["app-server"]] * 2)
        self.assertEqual(sum(c["model_call"] for c in calls), 0)
        dumped = json.dumps([asdict(item) for item in results])
        account = json.loads((FAKE / "appserver/account.json").read_text())["payload"]
        limits = json.loads((FAKE / "appserver/rate-limits.json").read_text())["payload"]
        for value in ("email", "accountId", "workspaceId", "workspaceRouting", "plan", str(home),
                      account["account"]["email"], account["workspaceRouting"]["accountId"],
                      account["workspaceRouting"]["workspaceId"], limits["accountId"]):
            self.assertNotIn(value, dumped)

    def test_snapshot_whitelists_fields_and_takes_worst_quota_window(self):
        adapter = self.stub()
        observed, = gather_access([adapter])
        self.assert_once(adapter)
        self.assertEqual(observed, replace(SEEN, models={FIRST: ("low", "medium")}))
        self.assertNotIn("secret", json.dumps(asdict(observed)))
        self.assertNotIn("private", json.dumps(asdict(observed)))
        with self.assertRaises(FrozenInstanceError):
            observed.auth = "missing"
        adapter = self.stub()
        adapter.quota.return_value["windows"] = [{"used_pct": 3}, {"used_pct": 97}]
        observed, = gather_access([adapter])
        self.assertEqual(route("executor", "risky", [observed], load_roles())["skipped"][0]["reason"],
                         "quota_exhausted")

    def test_each_probe_failure_never_raises_or_suppresses_other_probes(self):
        for method in ("detect", "auth_status", "list_models", "quota"):
            with self.subTest(method=method):
                adapter = self.stub()
                getattr(adapter, method).side_effect = RuntimeError("private@example.invalid secret-account")
                observed, = gather_access([adapter])
                self.assert_once(adapter)
                self.assertNotIn("private", json.dumps(asdict(observed)))
                if method == "detect":
                    self.assertFalse(observed.present)
                elif method == "auth_status":
                    self.assertEqual(observed.auth, "unknown")
                elif method == "list_models":
                    self.assertIsNone(observed.models)
                else:
                    self.assertIsNone(observed.quota_allowed)
                    self.assertIsNone(observed.quota_used_pct)

    def test_empty_unseen_malformed_models_and_unknown_quota(self):
        for payload, expected in (({"source": "none", "models": [{"id": FIRST}]}, None),
                                  ({"source": "app-server", "models": []}, {}),
                                  ({"source": "app-server", "models": [{"id": FIRST}]}, {FIRST: ()}),
                                  ({"source": "app-server", "models": [{"id": 1}]}, None),
                                  (None, None)):
            with self.subTest(payload=payload):
                adapter = self.stub()
                adapter.list_models.return_value = payload
                adapter.quota.return_value = {"source": "none", "allowed": False, "windows": [{"used_pct": 100}]}
                observed, = gather_access([adapter])
                self.assertEqual(observed.models, expected)
                self.assertIsNone(observed.quota_allowed)
                self.assertIsNone(observed.quota_used_pct)
        for value in (True, float("nan"), float("inf"), -1, "97", None):
            adapter = self.stub()
            adapter.quota.return_value["windows"] = [{"used_pct": value}]
            self.assertIsNone(gather_access([adapter])[0].quota_used_pct)

    def test_multiple_adapters_and_missing_manifest_do_not_guess_identity(self):
        one, two = self.stub(), self.stub()
        two.manifest = None
        access = gather_access([one, two])
        self.assertEqual(len(access), 2)
        self.assertFalse(access[1].present)
        self.assertEqual((access[1].tool, access[1].family), ("", ""))
        self.assert_once(one)
        self.assert_once(two)
        self.assertEqual(gather_access([]), [])


if __name__ == "__main__":
    unittest.main()
