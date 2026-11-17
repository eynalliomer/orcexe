"""Exercise scripts/check_marketplace.py on the checkout and on broken temp copies."""

import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("check_marketplace", ROOT / "scripts" / "check_marketplace.py")
check_marketplace = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(check_marketplace)


def _edit_json(path, change):
    data = json.loads(path.read_text(encoding="utf-8"))
    change(data)
    path.write_text(json.dumps(data), encoding="utf-8")


def _skill(text):
    return lambda root: (root / "plugins/orcexe/skills/orcexe/SKILL.md").write_text(text, encoding="utf-8")


GOOD_FRONT = "---\nname: orcexe\ndescription: Use when testing\n  a folded line.\n---\n"
MARKETPLACE = ".claude-plugin/marketplace.json"
PLUGIN_JSON = "plugins/orcexe/.claude-plugin/plugin.json"

# Each mutation of a valid copy and a fragment of the expected message.
FAILURES = {
    "marketplace_invalid_json": (lambda r: (r / MARKETPLACE).write_text("{", encoding="utf-8"),
                                 "cannot read JSON"),
    "marketplace_missing_owner": (lambda r: _edit_json(r / MARKETPLACE, lambda d: d.pop("owner")),
                                  "missing required key 'owner'"),
    "entry_missing_version": (lambda r: _edit_json(r / MARKETPLACE, lambda d: d["plugins"][0].pop("version")),
                              "plugins[0]: missing required key 'version'"),
    "source_missing": (lambda r: _edit_json(r / MARKETPLACE,
                                            lambda d: d["plugins"][0].update(source="./plugins/missing")),
                       "source './plugins/missing'"),
    "plugin_missing_description": (lambda r: _edit_json(r / PLUGIN_JSON, lambda d: d.pop("description")),
                                   "plugin.json: missing required key 'description'"),
    "plugin_version_mismatch": (lambda r: _edit_json(r / PLUGIN_JSON, lambda d: d.update(version="9.9.9")),
                                "version mismatch"),
    "package_version_mismatch": (lambda r: (r / "plugins/orcexe/src/orcexe/__init__.py").write_text(
                                     '"""x."""\n__version__ = "0.0.0-test"\n', encoding="utf-8"),
                                 "orcexe.__version__=0.0.0-test"),
    "launcher_missing": (lambda r: (r / "plugins/orcexe/bin/orcexe").unlink(), "launcher missing"),
    "skill_no_front_matter": (_skill("# orcexe\n"), "missing or invalid front matter"),
    "skill_no_name": (_skill("---\ndescription: Use when testing\n---\n"), "lacks 'name'"),
    "skill_no_description": (_skill("---\nname: orcexe\n---\n"), "lacks 'description'"),
    "skill_bad_description": (_skill("---\nname: orcexe\ndescription: Helps with tools\n---\n"),
                              "must start with 'Use when'"),
    "skill_too_long": (_skill(GOOD_FRONT + "line\n" * 156), "161 lines (max 160)"),
    "skills_absent": (lambda r: shutil.rmtree(str(r / "plugins/orcexe/skills")), "no skills/*/SKILL.md"),
    "command_no_description": (lambda r: (r / "plugins/orcexe/commands/orcexe.md").write_text(
                                   "---\nargument-hint: x\n---\nbody\n", encoding="utf-8"),
                               "commands/orcexe.md: front matter lacks 'description'"),
}


class CheckMarketplaceTests(unittest.TestCase):
    def copy(self):
        tmp = tempfile.TemporaryDirectory(prefix="orcexe-marketplace-")
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        ignore = shutil.ignore_patterns("__pycache__")
        shutil.copytree(str(ROOT / ".claude-plugin"), str(root / ".claude-plugin"))
        shutil.copytree(str(ROOT / "plugins" / "orcexe"), str(root / "plugins" / "orcexe"), ignore=ignore)
        return root

    def run_main(self, root):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = check_marketplace.main(["--root", str(root)])
        return code, output.getvalue()

    def test_checkout_passes(self):
        errors, summary = check_marketplace.check(ROOT)
        self.assertEqual(errors, [])
        self.assertIn("1 skill(s), 1 command(s)", summary)

    def test_valid_copy_passes_with_folded_description(self):
        root = self.copy()
        _skill(GOOD_FRONT + "body\n" * 155)(root)
        code, output = self.run_main(root)
        self.assertEqual(code, 0, output)
        self.assertTrue(output.startswith("OK    plugin orcexe "))

    def test_each_failure_exits_one_with_message(self):
        for name, (mutate, expected) in FAILURES.items():
            with self.subTest(name=name):
                root = self.copy()
                mutate(root)
                code, output = self.run_main(root)
                self.assertEqual(code, 1, output)
                self.assertIn("FAIL  ", output)
                self.assertIn(expected, output)

    def test_front_matter_parsing(self):
        parse = check_marketplace.front_matter
        self.assertEqual(parse('---\nname: x\nargument-hint: "a \\"b\\" | c"\n---\n'),
                         {"name": "x", "argument-hint": 'a "b" | c'})
        self.assertEqual(parse("---\ndescription: Use when\n  folded\n---\n"), {"description": "Use when folded"})
        for text in ("", "name: x\n", "---\nname: x\n", "---\n  orphan\n---\n", "---\nno colon\n---\n"):
            self.assertIsNone(parse(text), text)


if __name__ == "__main__":
    unittest.main()
