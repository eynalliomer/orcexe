"""Every shipped manifest/profile must keep bypass options out of argv."""

import ast
from pathlib import Path
import re
import tempfile
from unittest import mock

from tests.security import ROOT, SOURCE, PrivateHomes, SecurityFixture
from orcexe.manifest import GLOBAL_FORBIDDEN_ARGV, Manifest, ManifestError, load_manifest


def forbidden_token(token, patterns):
    # Independent of manifest._check_forbidden: include --flag=value spellings.
    return any(token.startswith(pattern[:-1]) if pattern.endswith("*") else
               token == pattern or token.startswith(pattern + "=") for pattern in patterns)


class ForbiddenArgvTests(PrivateHomes):
    def test_all_manifests_profiles_and_hostile_placeholder_values(self):
        manifests = sorted((ROOT / "plugins/orcexe/adapters").glob("*.yaml"))
        self.assertTrue(manifests)
        with tempfile.TemporaryDirectory(prefix="orcexe-argv-") as temporary:
            directory = str(Path(temporary).resolve())
            for path in manifests:
                manifest = load_manifest(path)
                patterns = (*GLOBAL_FORBIDDEN_ARGV, *manifest.data["exec"]["forbidden_argv"])
                for profile in manifest.data["exec"]["profiles"]:
                    values = dict(workdir=directory, run_dir=directory, final=directory + "/final.md",
                                  model="representative-model", effort=manifest.data["exec"]["effort"]["values"][0],
                                  prompt="Read the brief and finish the task.")
                    needed = manifest.placeholders(profile)
                    def build(candidate):
                        return manifest.build_argv(profile, **{key: candidate[key] for key in needed})
                    with self.subTest(manifest=path.name, profile=profile):
                        argv = build(values)
                        self.assertFalse([token for token in argv if forbidden_token(token, patterns)])
                    attacks = [pattern[:-1] + "security-bypass" if pattern.endswith("*") else pattern
                               for pattern in patterns]
                    for name in needed:
                        hostile = ([value for value in attacks if name != "prompt" or value.startswith("-")]
                                   + ["--dangerously-skip-permissions", "--yolo", "--force=x"]
                                   if name in ("model", "prompt") else ["-hostile-workdir"]
                                   if name in ("workdir", "run_dir", "final") else ["--yolo"])
                        for value in hostile:
                            with self.subTest(manifest=path.name, profile=profile, name=name, value=value):
                                candidate = dict(values, **{name: value})
                                with self.assertRaises(ManifestError):
                                    build(candidate)
                    # Flags embedded in ordinary free text remain one operand;
                    # they must never be split into additional option elements.
                    prompt = "Discuss --yolo, --force and --dangerously-skip-permissions in this task."
                    with self.subTest(manifest=path.name, profile=profile, prompt="embedded flags"):
                        argv = build(dict(values, prompt=prompt))
                        operands = [token for token in argv if prompt in token]
                        self.assertEqual(len(operands), 1)
                        self.assertFalse(operands[0].lstrip().startswith("-"))
                        self.assertFalse([token for token in argv if forbidden_token(token, patterns)])

    def test_orchestration_source_contains_no_literal_bypass_flags(self):
        patterns = set(GLOBAL_FORBIDDEN_ARGV)
        for path in (ROOT / "plugins/orcexe/adapters").glob("*.yaml"):
            patterns.update(load_manifest(path).data["exec"]["forbidden_argv"])
        violations = []
        for module in ("do.py", "run.py", "gate_lite.py"):
            path = SOURCE / "orcexe" / module
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {id(node.body[0].value) for node in ast.walk(tree)
                          if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                          and node.body and isinstance(node.body[0], ast.Expr)
                          and isinstance(node.body[0].value, ast.Constant)
                          and isinstance(node.body[0].value.value, str)}
            for node in ast.walk(tree):
                if id(node) in docstrings:
                    continue
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    for token in re.findall(r"[A-Za-z0-9_=-]+", node.value):
                        if forbidden_token(token, patterns):
                            violations.append("{}:{}: {}".format(path.relative_to(ROOT), node.lineno, token))
        self.assertEqual(violations, [], "\n".join(violations))


class PromptOperandTests(SecurityFixture):
    def test_prompt_is_one_operand_and_real_pointer_is_never_forbidden(self):
        # T08: free text is opaque, even when it equals a forbidden bare token.
        directory = str(self.root)
        for path in sorted((ROOT / "plugins/orcexe/adapters").glob("*.yaml")):
            manifest = load_manifest(path)
            for profile in manifest.data["exec"]["profiles"]:
                with self.subTest(manifest=path.name, profile=profile):
                    values = dict(workdir=directory, run_dir=directory,
                                  final=directory + "/final.md", model="model",
                                  effort=manifest.data["exec"]["effort"]["values"][0],
                                  prompt="bypassPermissions")
                    needed = manifest.placeholders(profile)
                    argv = manifest.build_argv(profile, **{key: values[key] for key in needed})
                    self.assertEqual(sum(token.count(values["prompt"]) for token in argv), 1)
                    self.assertEqual(sum(values["prompt"] in token for token in argv), 1)

        # Observe the actual sentence built by adapters/base.py for every tool.
        observed = []
        build = Manifest.build_argv

        def capture(manifest, profile, **values):
            argv = build(manifest, profile, **values)
            # Claude/Cursor also validate grants with an unsent placeholder.
            if values["prompt"] != "Preflight":
                observed.append((manifest, values["prompt"], argv))
            return argv

        with mock.patch.object(Manifest, "build_argv", capture):
            self.run_flows()
        self.assertEqual(len(observed), 4)
        for manifest, prompt, argv in observed:
            with self.subTest(tool=manifest.tool):
                self.assertTrue(prompt.startswith("Read the task brief at "))
                self.assertTrue(prompt.endswith(" and carry it out. Follow its rules exactly."))
                self.assertEqual(sum(token.count(prompt) for token in argv), 1)
                patterns = (*GLOBAL_FORBIDDEN_ARGV, *manifest.data["exec"]["forbidden_argv"])
                for pattern in patterns:
                    self.assertFalse(prompt.casefold().startswith(pattern.rstrip("*").casefold()))
                self.assertFalse([token for token in argv if forbidden_token(token, patterns)])
