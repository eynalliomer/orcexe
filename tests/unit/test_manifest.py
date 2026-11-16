"""T08 schema, evidence, safety and mutation contract; no CLI is executed."""

import ast
import copy
from dataclasses import FrozenInstanceError
from pathlib import Path
import time
import unittest

from orcexe.manifest import (
    GLOBAL_FORBIDDEN_ARGV, Manifest, ManifestError, load_manifest,
    meets_min_version, parse_version, validate,
)
from orcexe.miniyaml import load_path


ROOT = Path(__file__).resolve().parents[2]
CODEX = ROOT / "plugins/orcexe/adapters/codex.yaml"
FIXTURES = ROOT / "tests/fixtures/manifest"
VALUES = dict(workdir="/repo with spaces", model="gpt-6.1-terra", effort="high",
              final="/tmp/final result.txt", prompt="Explain this code")


def mappings(value, path=()):
    """Independently enumerate mapping locations, including list elements."""
    if isinstance(value, dict):
        yield path, value
        for key, item in value.items():
            yield from mappings(item, path + (key,))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            yield from mappings(item, path + (index,))


def at(data, path):
    for key in path:
        data = data[key]
    return data


def location(path):
    result = ""
    for part in path:
        if isinstance(part, int):
            result += "[{}]".format(part)
        else:
            result += ("." if result else "") + part
    return result


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.raw = load_path(CODEX)

    def assert_invalid(self, data, path, exact=True):
        with self.assertRaises(ManifestError) as caught:
            validate(data)
        error = caught.exception
        if exact:
            self.assertEqual(error.path, path)
        else:
            self.assertTrue(error.path == path or error.path.startswith(path + ".") or
                            error.path.startswith(path + "["), (error.path, path))
        self.assertIn(error.path, str(error))
        self.assertTrue(error.message)
        return error

    def test_codex_exact_argv(self):
        manifest = load_manifest(CODEX)
        self.assertEqual((manifest.tool, manifest.family), ("codex", "openai"))
        for profile, sandbox in (("write", "workspace-write"), ("review", "read-only")):
            with self.subTest(profile=profile):
                self.assertEqual(manifest.build_argv(profile, **VALUES), [
                    "codex", "exec", "-C", "/repo with spaces", "-m", "gpt-6.1-terra",
                    "-c", "model_reasoning_effort=high", "--json", "-o",
                    "/tmp/final result.txt", "Explain this code", "-s", sandbox,
                ])

    def test_deep_read_only_detached_copy(self):
        manifest = Manifest(self.raw)
        self.raw["exec"]["argv"][0] = "changed"
        self.raw["evidence"]["exec.argv"] = "?"
        self.assertEqual(manifest.data["exec"]["argv"][0], "codex")
        self.assertEqual(manifest.evidence("exec.argv"), "D+Y")
        with self.assertRaises(TypeError):
            manifest.data["tool"] = "changed"
        with self.assertRaises(TypeError):
            manifest.data["exec"]["argv"][0] = "changed"
        with self.assertRaises(TypeError):
            manifest.data["side_effects"][0]["handler"] = "changed"
        with self.assertRaises(FrozenInstanceError):
            manifest.data = {}
        argv = manifest.build_argv("write", **VALUES)
        argv[0] = "changed"
        self.assertEqual(manifest.build_argv("write", **VALUES)[0], "codex")

    def test_evidence_unknown_and_longest_prefix(self):
        manifest = load_manifest(CODEX)
        for path in ("quota.exec_exhaustion", "events.model_actual", "exit_codes.runtime"):
            self.assertTrue(manifest.is_uncertain(path), path)
        self.assertEqual(manifest.evidence("exec.profiles.review.extra"), "D+Y")
        fixture = load_manifest(FIXTURES / "valid.yaml")
        self.assertTrue(fixture.is_uncertain("exec.argv"))
        self.assertTrue(fixture.is_uncertain("uncovered.path"))
        self.assertEqual(fixture.evidence("exec.profiles.review.read_only"), "D+Y")
        # Prefixes must end on a dot boundary, never a partial name.
        self.assertTrue(fixture.is_uncertain("exec.profiles.reviewed"))
        self.assertEqual(fixture.evidence("windows.native"), "D")
        self.assertEqual(fixture.evidence("windowshop"), "?")

    def test_versions(self):
        self.assertEqual(parse_version("0.163.0"), (0, 163, 0))
        self.assertEqual(parse_version("codex-cli 0.163.0"), (0, 163, 0))
        self.assertEqual(parse_version("0.164.0-alpha.1"), (0, 164, 0))
        cases = [
            ("0.163.0", "0.159.0", True), ("0.99.0", "0.159.0", False),
            ("0.159.0", "0.159.0", True), ("1.0.0", "0.159.0", True),
            ("0.164.0-alpha.1", "0.164.0", False),
            ("0.164.0", "0.164.0-alpha.1", True),
            ("0.164.0-alpha.2", "0.164.0-alpha.10", False),
            ("0.164.0-beta", "0.164.0-alpha.10", True),
            ("0.164.0-alpha", "0.164.0-alpha.1", False),
            ("0.164.0+build.1", "0.164.0", True), ("1.0", "1.0.0", True),
        ]
        for found, minimum, expected in cases:
            with self.subTest(found=found, minimum=minimum):
                self.assertIs(meets_min_version(found, minimum), expected)
        for text in ("", "not a version", "123", None, True):
            with self.subTest(text=text):
                with self.assertRaises(ManifestError):
                    parse_version(text)
                with self.assertRaises(ManifestError):
                    meets_min_version(text, "0.159.0")
                with self.assertRaises(ManifestError):
                    meets_min_version("0.159.0", text)

    def test_hostile_substitutions(self):
        manifest = load_manifest(CODEX)
        for name in VALUES:
            for value in ("", "-c x", "  --x", "--dangerously-new-flag", "x\0y"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ManifestError) as caught:
                        manifest.build_argv("write", **dict(VALUES, **{name: value}))
                    self.assertEqual(caught.exception.path, "exec.argv." + name)
        for name in ("model", "effort"):
            for value in ("-c", "a b", "a\tb", "a\nb", "a\u00a0b",
                          "x --yolo", "x=danger-full-access", "x=Danger-Full-Access",
                          "bypassPermissions", "BYPASSPERMISSIONS"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ManifestError):
                        manifest.build_argv("write", **dict(VALUES, **{name: value}))
        with self.assertRaisesRegex(ManifestError, "unsupported effort"):
            manifest.build_argv("write", **dict(VALUES, effort="super-high"))

    def test_model_and_effort_forbidden_values(self):
        for value in ("Danger-Full-Access", "BYPASSPERMISSIONS", "x=Danger-Full-Access"):
            self.raw["exec"]["effort"]["values"].append(value)
            manifest = Manifest(self.raw)
            for name in ("model", "effort"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ManifestError, "forbidden") as caught:
                        manifest.build_argv("write", **dict(VALUES, **{name: value}))
                    self.assertEqual(caught.exception.path, "exec.argv." + name)

    def test_prompt_free_text_and_length(self):
        manifest = load_manifest(CODEX)
        for profile in ("write", "review"):
            for prompt in ("Never pass --yolo or danger-full-access.",
                           "Never set sandbox_mode=workspace-write or --sandbox=write.",
                           "Explain --locally-forbidden and BYPASSPERMISSIONS.",
                           "  Explain this\nwith whitespace", " ", "\U0001f600" * 8000):
                with self.subTest(profile=profile, prompt=prompt[:80]):
                    argv = manifest.build_argv(profile, **dict(VALUES, prompt=prompt))
                    self.assertEqual(argv[11], prompt)
                    self.assertEqual(len(argv), 14)
        with self.assertRaises(ManifestError) as caught:
            manifest.build_argv("write", **dict(VALUES, prompt="x" * 8001))
        self.assertEqual(caught.exception.path, "exec.argv.prompt")

    def test_absolute_path_values(self):
        manifest = load_manifest(CODEX)
        for name, index in (("workdir", 3), ("final", 10)):
            for value in ("/Volumes/EYN SSD/x", "/tmp/--yolo danger-full-access",
                          "/tmp/--sandbox=write sandbox_mode=workspace-write"):
                for profile in ("write", "review"):
                    with self.subTest(name=name, value=value, profile=profile):
                        argv = manifest.build_argv(profile, **dict(VALUES, **{name: value}))
                        self.assertEqual(argv[index], value)
            for value in ("relative/path", "./relative", "../relative", "~/repo", " /repo"):
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ManifestError) as caught:
                        manifest.build_argv("write", **dict(VALUES, **{name: value}))
                    self.assertEqual(caught.exception.path, "exec.argv." + name)

    def test_placeholder_values_and_extras(self):
        manifest = load_manifest(CODEX)
        for name in VALUES:
            values = dict(VALUES)
            del values[name]
            with self.subTest(missing=name), self.assertRaisesRegex(ManifestError, "missing"):
                manifest.build_argv("write", **values)
        with self.assertRaisesRegex(ManifestError, "unexpected"):
            manifest.build_argv("write", **dict(VALUES, typo="oops"))
        with self.assertRaisesRegex(ManifestError, "unknown profile"):
            manifest.build_argv("unsafe", **VALUES)
        with self.assertRaises(ManifestError):
            manifest.build_argv("write", **dict(VALUES, model=123))
        # Supplied text is not recursively interpreted as another placeholder.
        argv = manifest.build_argv("write", **dict(VALUES, prompt="Explain {model}"))
        self.assertEqual(argv[11], "Explain {model}")
        fixture = load_manifest(FIXTURES / "valid.yaml")
        self.assertEqual(fixture.build_argv("write", workdir="/repo", model="m", prompt="p")[-2:],
                         ["-s", "workspace-write"])
        with self.assertRaisesRegex(ManifestError, "unexpected"):
            fixture.build_argv("write", workdir="/repo", model="m", prompt="p", effort="high")

    def test_defence_in_depth(self):
        # Embedded substitutions can synthesize a forbidden token across a
        # template/value boundary, even though each piece is harmless alone.
        self.raw["exec"]["argv"][5] = "--{model}"
        manifest = Manifest(self.raw)
        for model in ("yolo", "YOLO"):
            with self.subTest(model=model), self.assertRaises(ManifestError) as caught:
                manifest.build_argv("write", **dict(VALUES, model=model))
            self.assertEqual(caught.exception.path, "exec.argv[5]")

    def test_embedded_free_text_values(self):
        self.raw["exec"]["argv"][10] = "output={final}"
        self.raw["exec"]["argv"][11] = "brief={prompt}"
        self.raw["exec"]["profiles"]["write"]["extra"].append("note={final}")
        manifest = Manifest(self.raw)
        values = dict(VALUES, final="/tmp/--yolo danger-full-access",
                      prompt="Never pass --yolo or set sandbox_mode=workspace-write.")
        for profile in ("write", "review"):
            argv = manifest.build_argv(profile, **values)
            self.assertEqual(argv[10], "output=" + values["final"])
            self.assertEqual(argv[11], "brief=" + values["prompt"])
            if profile == "write":
                self.assertEqual(argv[-1], "note=" + values["final"])

    def test_local_forbidden_values_and_nonexec_commands(self):
        fixture = load_manifest(FIXTURES / "valid.yaml")
        with self.assertRaises(ManifestError):
            fixture.build_argv("write", workdir="/repo", model="m", prompt="--locally-forbidden")
        self.assertIn("Explain --locally-forbidden", fixture.build_argv(
            "write", workdir="/repo", model="m", prompt="Explain --locally-forbidden"))
        for token in ("--option=unsafe=config", "--option='unsafe=config'",
                      "--option=UNSAFE=Config", "--option='UNSAFE=Config'"):
            data = copy.deepcopy(self.raw)
            data["exec"]["forbidden_argv"].append("unsafe=config")
            data["exec"]["argv"].append(token)
            self.assert_invalid(data, "exec.argv[12]")
        for path in (("binary",), ("version_cmd",), ("auth", "status_cmd"),
                     ("auth", "login_cmd"), ("auth", "device_cmd")):
            data = copy.deepcopy(self.raw)
            at(data, path).append("--force")
            self.assert_invalid(data, location(path) + "[{}]".format(len(at(data, path)) - 1))

    def test_review_rules(self):
        mutants = [
            (("exec", "profiles", "review", "read_only"), False),
            (("exec", "profiles", "review", "extra"), ["-s", "workspace-write"]),
            (("exec", "profiles", "review", "extra"), []),
            (("exec", "profiles", "review", "extra"), ["-s", "read-only", "-s", "workspace-write"]),
            (("exec", "profiles", "review", "extra"), ["-s", "read-only", "--sandbox=workspace-write"]),
            (("exec", "profiles", "review", "extra"), ["-s", "read-only", "-sworkspace-write"]),
            (("exec", "profiles", "review", "extra"), ["-s", "read-only", "-c", "sandbox_mode=workspace-write"]),
            (("exec", "profiles", "review", "extra"), ["-s", "read-only", "-csandbox_mode=workspace-write"]),
            (("exec", "profiles", "review", "extra"), ["-s", "read-only", "--config=sandbox_mode=workspace-write"]),
            (("exec", "profiles", "review", "extra"), ["--", "-s", "read-only"]),
            (("exec", "profiles", "review", "extra"), ["-o", "-s", "read-only"]),
        ]
        for path, value in mutants:
            with self.subTest(value=value):
                data = copy.deepcopy(self.raw)
                at(data, path[:-1])[path[-1]] = value
                self.assert_invalid(data, location(path), exact=False)
        self.raw["exec"]["argv"].extend(["-c", "sandbox_mode=workspace-write"])
        self.assert_invalid(self.raw, "exec.argv[13]")
        with self.assertRaises(ManifestError):
            load_manifest(CODEX).build_argv("review", **dict(VALUES, prompt="--"))
        for tool, family in (("claude-code", "anthropic"), ("cursor", "cursor")):
            data = load_path(CODEX)
            data.update(tool=tool, family=family)
            self.assert_invalid(data, "exec.profiles.review")

    def test_source_alternatives(self):
        alternatives = [
            {"source": "none"},
            {"source": "cmd", "cmd": ["example", "models"], "parse": "slug-dash-lines"},
            {"source": "stream", "event": "rate_limit_event"},
        ]
        for source in alternatives:
            for section in ("models", "quota"):
                with self.subTest(source=source, section=section):
                    data = load_path(FIXTURES / "valid.yaml")
                    if section == "quota":
                        data[section].update(source)
                    else:
                        data[section] = source
                    validate(data)
        self.raw["models"] = {"source": "none", "method": "model/list"}
        self.assert_invalid(self.raw, "models.method")

    def test_open_install_keys_and_scalar_login_conditions(self):
        data = load_path(FIXTURES / "valid.yaml")
        data["install"] = {"macos.linux": "display only"}
        data["auth"]["logged_in"] = {"json_field": "session", "equals": None}
        data["evidence"]["auth.logged_in"] = "D"
        validate(data)

    def test_schema_and_consistency_errors(self):
        cases = [
            (("schema",), "other"), (("tool",), "other"), (("family",), "cursor"),
            (("binary",), []), (("binary",), [123]), (("bin_env",), "PATH"),
            (("min_version",), 1.5), (("version_regex",), "("),
            (("version_regex",), r"\d+"), (("version_regex",), r"(\d+)(\d+)"),
            (("version_sample",), "no version"),
            (("version_sample",), "codex-cli nonsense"),
            (("verified_on", "version"), "none"), (("install",), {"npm": None}),
            (("auth", "logged_in", "exit_code"), True),
            (("auth", "logged_out"), {"exit_code": 0}),
            (("auth", "plan", "field"), "email"),
            (("auth", "allowed_fields"), ["planType", "email"]),
            (("models", "source"), "invented"), (("quota", "nullable_fields"), ["unknown"]),
            (("quota", "read_only_consumes_quota"), 0), (("exec", "stdin"), "inherit"),
            (("exec", "requires_git_repo"), "yes"), (("exec", "effort", "values"), []),
            (("events", "format"), "text"), (("events", "stream"), "stderr"),
            (("events", "success", "last_type"), "turn.failed"),
            (("events", "usage", "type"), "unknown"), (("exit_codes", "usage"), True),
            (("windows", "native"), 1), (("evidence", "exec.argv"), "X"),
            (("evidence", "quota.exec_exhaustion"), "D"),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                data = copy.deepcopy(self.raw)
                at(data, path[:-1])[path[-1]] = value
                # Several cross-field errors identify the dependent field.
                expected = {
                    ("install",): "install.npm",
                    ("evidence", "quota.exec_exhaustion"): "quota.exec_exhaustion",
                }.get(path, location(path))
                self.assert_invalid(data, expected, exact=False)
        for token in ("{unknown}", "{model!r}", "{model:>10}", "{model.name}", "{", "{{model}}"):
            data = copy.deepcopy(self.raw)
            data["exec"]["argv"].append(token)
            self.assert_invalid(data, "exec.argv[12]")
        self.raw["evidence"]["exec.arg"] = "D"
        self.assert_invalid(self.raw, "evidence.exec.arg")

    def test_bad_tree_and_load_errors(self):
        for data in (None, [], "manifest", 42):
            with self.assertRaises(ManifestError):
                validate(data)
        cycle = {}
        cycle["self"] = cycle
        self.assert_invalid(cycle, "self")
        for name in ("invalid-schema.yaml", "invalid-yaml.yaml"):
            with self.subTest(name=name), self.assertRaises(ManifestError):
                load_manifest(FIXTURES / name)
        with self.assertRaises(OSError):
            load_manifest(FIXTURES / "absent.yaml")

    def test_python39_syntax(self):
        for path in (ROOT / "plugins/orcexe/src/orcexe/manifest.py", Path(__file__)):
            ast.parse(path.read_text(encoding="utf-8"), feature_version=(3, 9))


class ManifestMutationTests(unittest.TestCase):
    setUp = ManifestTests.setUp
    assert_invalid = ManifestTests.assert_invalid

    def test_mutations(self):
        started = time.perf_counter()
        count = 0
        data = self.raw

        # Every schema key in the real manifest is required for its selected
        # variant. Install and evidence are intentionally dynamic mappings.
        for path, mapping in mappings(data):
            if path in (("install",), ("evidence",)):
                continue
            for key in mapping:
                mutant = copy.deepcopy(data)
                del at(mutant, path)[key]
                mutated = location(path + (key,))
                with self.subTest(kind="delete required", path=mutated):
                    self.assert_invalid(mutant, mutated)
                count += 1

        for key in data["evidence"]:
            mutant = copy.deepcopy(data)
            del mutant["evidence"][key]
            with self.subTest(kind="delete evidence", path=key):
                error = self.assert_invalid(mutant, key, exact=False)
                self.assertIn("coverage", error.message)
            count += 1

        forbidden = list(GLOBAL_FORBIDDEN_ARGV) + data["exec"]["forbidden_argv"]
        # Exercise both the prefix itself and future flags in that family.
        forbidden = [token[:-1] if token.endswith("*") else token for token in forbidden]
        forbidden.append("--dangerously-a-future-flag")
        argv_paths = [("exec", "argv"), ("exec", "profiles", "write", "extra"),
                      ("exec", "profiles", "review", "extra")]
        for token in forbidden + [token.upper() for token in forbidden] + ["Danger-Full-Access"]:
            for value in (token, "--option=" + token, "sandbox_mode=" + token):
                for path in argv_paths:
                    mutant = copy.deepcopy(data)
                    index = len(at(mutant, path))
                    at(mutant, path).append(value)
                    mutated = location(path + (index,))
                    with self.subTest(kind="forbidden", token=value, path=mutated):
                        self.assert_invalid(mutant, mutated)
                    count += 1

        for path, _ in mappings(data):
            mutant = copy.deepcopy(data)
            if path == ("install",):
                # Open str->str mapping: an unknown non-string value is invalid.
                value = 123
            elif path == ("evidence",):
                value = "D"  # Open paths must point at actual manifest data.
            else:
                value = "typo"
            at(mutant, path)["unknown_key"] = value
            mutated = location(path + ("unknown_key",))
            with self.subTest(kind="unknown key", path=mutated):
                self.assert_invalid(mutant, mutated)
            count += 1

        for path in argv_paths:
            mutant = copy.deepcopy(data)
            index = len(at(mutant, path))
            at(mutant, path).append("{unknown}")
            with self.subTest(kind="placeholder", path=path):
                self.assert_invalid(mutant, location(path + (index,)))
            count += 1
            for name in ("workdir", "model", "prompt"):
                mutant = copy.deepcopy(data)
                at(mutant, path).append("{" + name + "}")
                with self.subTest(kind="duplicate", path=path, name=name):
                    self.assert_invalid(mutant, location(path + (index,)))
                count += 1
        elapsed = time.perf_counter() - started
        print("\nManifest mutation proof: {} mutants rejected in {:.3f}s".format(count, elapsed))
        self.assertLess(elapsed, 5.0)


if __name__ == "__main__":
    unittest.main()
