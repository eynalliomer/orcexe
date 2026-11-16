"""Check executor environment allowlisting and unconditional secret denial."""

import os
from pathlib import Path
import sys
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "orcexe" / "src"
sys.path.insert(0, str(SOURCE))

from orcexe.envfilter import child_env


class EnvfilterTests(unittest.TestCase):
    def test_allowlist_passes_through_and_others_are_omitted(self):
        names = (
            "PATH HOME USER LOGNAME SHELL LANG LC_ALL LC_CTYPE TERM TMPDIR TZ "
            "CODEX_HOME CLAUDE_CONFIG_DIR XDG_CONFIG_HOME XDG_CACHE_HOME XDG_DATA_HOME"
        ).split()
        expected = {name: "ordinary-setting" for name in names}
        base = dict(expected, UNRELATED="ordinary-setting")
        env, dropped = child_env(base)
        self.assertEqual(env, expected)
        self.assertEqual(dropped, [])

    def test_windows_names_pass_through(self):
        names = "SystemRoot SYSTEMROOT APPDATA LOCALAPPDATA USERPROFILE ComSpec PATHEXT WINDIR".split()
        base = {name: "ordinary-setting" for name in names}
        env, dropped = child_env(base)
        self.assertEqual(env, base)
        self.assertEqual(dropped, [])

    def test_deny_beats_allow_extra_case_insensitively(self):
        names = [
            "OPENAI_API_KEY", "SERVICE_TOKEN", "service_secret", "service_PASSWORD",
            "AWS_REGION", "aws_profile", "OPENAI_MONKEY_SETTING", "ANTHROPIC_KEY",
            "CURSOR_SIGNING_KEY_ID", "GITHUB_PRIVATE_KEY", "GH_KEY_PATH",
        ]
        base = {name: "FAKE-SENSITIVE-VALUE" for name in names}
        base.update(PATH="ordinary-path", CUSTOM="ordinary-setting")
        env, dropped = child_env(base, allow_extra=names + ["CUSTOM"])
        self.assertEqual(set(env), {"PATH", "CUSTOM"})
        self.assertTrue(dropped == sorted(names), "Unexpected dropped environment names")
        self.assertTrue(all(name in base for name in dropped), "Dropped entries must be names")
        self.assertTrue("FAKE-SENSITIVE-VALUE" not in dropped, "Values must not be reported")

    def test_denied_names_are_reported_even_without_allow_extra(self):
        env, dropped = child_env({"Z_TOKEN": "FAKE-VALUE", "A_SECRET": "FAKE-VALUE", "OTHER": "ordinary"})
        self.assertTrue(env == {}, "Denied entries remained in the environment")
        self.assertTrue(dropped == ["A_SECRET", "Z_TOKEN"], "Unexpected dropped environment names")

    def test_deny_patterns_do_not_overreach(self):
        base = {"AWS": "ordinary", "TOKENIZER": "ordinary", "TOKENS": "12000", "KEYBOARD": "ordinary", "CUSTOM": "ordinary"}
        env, dropped = child_env(base, allow_extra=base)
        self.assertEqual(env, base)
        self.assertEqual(dropped, [])

    def test_base_is_not_mutated_and_result_is_new(self):
        base = {"PATH": "ordinary-path", "OPENAI_API_KEY": "FAKE-VALUE"}
        original = base.copy()
        env, _ = child_env(base)
        self.assertIsNot(env, base)
        env["PATH"] = "changed"
        self.assertTrue(base == original, "Input environment was mutated")

    def test_default_uses_current_environment(self):
        with mock.patch.dict(os.environ, {"PATH": "ordinary-path", "AWS_REGION": "ordinary"}, clear=True):
            env, dropped = child_env()
            self.assertEqual(env, {"PATH": "ordinary-path"})
            self.assertEqual(dropped, ["AWS_REGION"])
            self.assertIn("AWS_REGION", os.environ)

    def test_empty_base_does_not_fall_back_to_os_environ(self):
        self.assertEqual(child_env({}), ({}, []))

    def test_required_secret_names_are_denied_with_and_without_allow_extra(self):
        names = [
            "API_KEY", "TOKEN", "SECRET", "PASSWORD", "STRIPE_SECRET_KEY",
            "AZURE_OPENAI_KEY", "GITHUB_PAT", "GH_PAT", "PGPASSWORD", "MY_PRIVATE_KEY",
            "DB_PASSWD", "GOOGLE_APPLICATION_CREDENTIALS", "DATABASE_URL", "CODEX_API_KEY",
            "CURSOR_API_KEY", "GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
            "ANTHROPIC_AUTH_TOKEN", "APIKEY", "KEY", "CREDENTIAL", "CREDENTIALS",
            "SERVICE_TOKEN_LIMIT", "SERVICE_API_KEY_PATH", "SERVICE_PASSWORD_FILE",
        ]
        names += [name.lower() for name in names]
        safe = {name: "ordinary" for name in ("PATH", "HOME", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "TMPDIR", "LANG")}
        base = dict.fromkeys(names, "FAKE-VALUE")
        base.update(safe)
        for extra in ((), names):
            env, dropped = child_env(base, allow_extra=extra)
            self.assertTrue(env == safe, "Secret names passed the environment filter")
            self.assertTrue(dropped == sorted(names), "Unexpected dropped environment names")

    def test_allow_extra_string_is_rejected(self):
        with self.assertRaisesRegex(TypeError, "allow_extra.*str"):
            child_env({}, allow_extra="PATH")

    def test_windows_allowlist_and_extras_are_case_insensitive(self):
        base = {name: "ordinary" for name in ("COMSPEC", "systemroot", "path", "temp", "tmp", "Custom")}
        with mock.patch("orcexe.envfilter.os.name", "nt"):
            env, dropped = child_env(base, allow_extra=["CUSTOM"])
        self.assertEqual(env, base)
        self.assertEqual(dropped, [])

    def test_posix_allowlist_and_extras_remain_case_sensitive(self):
        base = {name: "ordinary" for name in ("PATH", "path", "Custom", "CUSTOM", "TEMP", "TMP")}
        with mock.patch("orcexe.envfilter.os.name", "posix"):
            env, dropped = child_env(base, allow_extra=["CUSTOM"])
        self.assertEqual(set(env), {"PATH", "CUSTOM", "TEMP", "TMP"})
        self.assertEqual(dropped, [])
