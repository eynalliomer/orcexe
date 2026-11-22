"""Host evidence distinguishes file/process observations from declarations."""

from pathlib import Path
from itertools import product
import tempfile
import unittest

from orcexe import hosts



class HostTests(unittest.TestCase):
    def test_innermost_host_all_marker_combinations_names_only(self):
        class NamesOnly(dict):
            def __getitem__(self, key):
                raise AssertionError("host value read")
            def get(self, key, default=None):
                raise AssertionError("host value read")
        names = ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CLAUDECODE")
        for flags in product((False, True), repeat=3):
            for value in ("", "private-value"):
                env = NamesOnly({name: value for name, flag in zip(names, flags) if flag})
                with self.subTest(flags=flags, empty=not value):
                    host = hosts.detect_host(env=env, subprocess_ok=True)
                    marker = next((name for name in names if name in env), None)
                    self.assertEqual(host["tool"], "codex" if any(flags[:2]) else
                                     "claude-code" if flags[2] else "none")
                    self.assertEqual(host["detected_by"], "env:" + marker if marker else "none")
                    if any(flags[:2]):
                        self.assertEqual(len(host["capabilities"]), 7)
                        self.assertTrue(all(c["status"] == "info" and c["evidence"] == "declared"
                                            for c in host["capabilities"]))
                    self.assertNotIn("private-value", str(host))

    def test_presence_only_and_all_capabilities(self):
        class NamesOnly(dict):
            def __getitem__(self, key):
                raise AssertionError("host value read")
            def get(self, key, default=None):
                raise AssertionError("host value read")
        for value in ("", "false", "synthetic-private-value"):
            host = hosts.detect_host(env=NamesOnly(CLAUDECODE=value), subprocess_ok=True)
            self.assertEqual(host["tool"], "claude-code")
            self.assertEqual(host["detected_by"], "env:CLAUDECODE")
            self.assertEqual([c["code"] for c in host["capabilities"]], ["H" + str(n) for n in range(1, 8)])
            self.assertEqual(host["capabilities"][0]["evidence"], "measured")
            self.assertTrue(all(c["status"] == "info" and c["evidence"] == "declared"
                                for c in host["capabilities"][2:]))

    def test_unknown_host_and_failed_subprocess_are_not_measured(self):
        host = hosts.detect_host(env={"CLAUDE_CODE_ENTRYPOINT": "ignored", "CODEX_HOME": "ignored"}, subprocess_ok=True)
        self.assertEqual(host["tool"], "none")
        self.assertEqual(host["detected_by"], "none")
        self.assertEqual([c["code"] for c in host["capabilities"]], ["H1", "H2"])
        self.assertTrue(all(c["status"] == "info" for c in host["capabilities"]))
        host = hosts.detect_host(env={"CLAUDECODE": ""}, subprocess_ok=False)
        self.assertEqual(host["capabilities"][0]["evidence"], "declared")

    def test_skill_file_measured_only_when_it_exists(self):
        with tempfile.TemporaryDirectory(prefix="orcexe-hosts-") as directory:
            path = Path(directory) / "SKILL.md"
            for exists in (False, True):
                if exists:
                    path.write_text("fixture skill")
                capability = hosts.detect_host(env={"CLAUDECODE": ""}, skill_path=path)["capabilities"][1]
                self.assertEqual(capability["evidence"], "measured" if exists else "declared")
                self.assertEqual(capability["status"], "pass" if exists else "info")
