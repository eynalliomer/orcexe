"""K6 vocabulary additions retain the closed grammar and safety boundaries."""

import itertools
from pathlib import Path
import sys
import unicodedata
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe.classify import RiskMap, Signal, classify


# English and Turkish spellings of four intents, including existing variants.
FORMS = (
    ("doc", "update_doc", "Update {target}"),
    ("doc", "update_doc", "{target}'yi güncelle"),
    ("doc", "add_example", "Add an example to {target}"),
    ("doc", "add_example", "{target}'ye örnek ekle"),
    ("doc", "add_section", "Add an installation example to {target}"),
    ("doc", "add_section", "Add a usage example to {target}"),
    ("doc", "add_section", "{target}'ye kurulum örneği ekle"),
    ("doc", "add_section", "{target}'ye kullanım örneği ekle"),
    ("source", "fix_docstring", "Fix the docstring in {target}"),
    ("source", "fix_docstring", "Fix the docstring of {target}"),
    ("source", "fix_docstring", "{target} docstring düzelt"),
    ("doc", "clarify_paragraph", "Clarify {target}"),
    ("doc", "clarify_paragraph", "{target}'yi netleştir"),
    ("doc", "clarify_paragraph", "{target}'deki paragrafı netleştir"),
)


def tasks(target_class, template):
    path = "docs/guide.md" if target_class == "doc" else "src/widget.py"
    for quote in ("", "`"):
        yield path, template.format(target=quote + path + quote)


class FastVocabularyTests(unittest.TestCase):
    def test_bilingual_forms_with_bare_and_backtick_targets(self):
        for target_class, intent, template in FORMS:
            for path, task in tasks(target_class, template):
                with self.subTest(task=task):
                    result = classify(task, [path])
                    self.assertEqual(result.lane, "fast", result)
                    self.assertEqual(result.targets, (path,))
                    self.assertEqual(result.signals, tuple(sorted((
                        Signal("intent", intent), Signal("target_class", target_class),
                        Signal("target_file", path)), key=lambda s: (s.id, s.detail))))

    def test_additional_bounded_articles_and_turkish_suffixes(self):
        for task in ("Update README.md", "README.md güncelle", "Clarify README.md",
                     "README.md netleştir", "README.md örnek ekle",
                     "Add an usage example to README.md",
                     "Fix docstring in src/widget.py", "Fix docstring of src/widget.py",
                     "README.md’ye örneği ekle", "README.md'e örnek ekle"):
            with self.subTest(task=task):
                self.assertEqual(classify(task, ["README.md", "src/widget.py"]).lane, "fast")

    def test_no_extra_words_clauses_targets_or_shell_syntax(self):
        extras = ("please", "now", "also", "sudo", "42", "usage", "of", "an",
                  "and fix docs/other.md", "ve src/other.py", "docs/guide.md",
                  "src/widget.py", "https://example.test/guide.md", "`missing.md`",
                  ";", "&& true", "| true", "$(true)", "> out.md", "\\", "# note")
        for target_class, _, template in FORMS:
            for path, task in tasks(target_class, template):
                if "`" in task:
                    # Backticks delimit a target, not adjacent grammar words.
                    for joined in (task.replace(" `", "`"), task.replace("` ", "`")):
                        if joined != task:
                            with self.subTest(task=joined):
                                self.assertNotEqual(classify(joined, [path]).lane, "fast")
                for extra in extras:
                    for extended in (task + " " + extra, extra + " " + task):
                        with self.subTest(task=extended):
                            result = classify(extended, [path, "docs/other.md", "src/other.py"])
                            self.assertNotEqual(result.lane, "fast", result)

    def test_target_classes_and_denied_paths(self):
        denied = (
            "docs/SECURITY.md", "docs/LICENSE.md", "docs/LICENCE.md", "docs/COPYING.txt",
            "docs/NOTICE.rst", "docs/manifest.txt", "requirements.txt", "constraints-dev.txt",
            "package.json", "pyproject.toml", "Cargo.lock", "notes.lock.md", "config/.env.md",
            "Dockerfile.md", "docs/secrets.md", "docs/payments/guide.md", "docs/auth/guide.md",
            ".github/workflows/guide.md", "nested/.github/guide.md", ".circleci/guide.md",
            "db/migrations/guide.md", "src/migrate/widget.py", "src/security/widget.py",
            "src/licence.py", "setup.py", "conftest.py", "vite.config.ts",
            "docs/i18n/en.txt", "locales/en.md", "src/translations/en.py", "messages.po",
        )
        directories = ("scripts", "bin", "tools", "hooks", ".husky", ".git", "ci",
                       "deploy", "infra", "ops", "vendor", "third_party", "third-party",
                       "node_modules", ".venv", "venv", "env", "site-packages", "dist",
                       "build", "out", "target", ".next", "coverage", "__generated__",
                       "generated", "gen", "i18n", "l10n", "locale", "locales", "translations")
        denied += tuple("nested/" + directory + "/" + name
                        for directory, name in itertools.product(directories, ("guide.md", "widget.py")))
        for target_class, _, template in FORMS:
            wrong_class = "src/widget.py" if target_class == "doc" else "docs/guide.md"
            for path in denied + (wrong_class,):
                # A basename must inherit all restrictions of the resolved path.
                for mention in {path, path.rsplit("/", 1)[-1]}:
                    for quote in ("", "`"):
                        task = template.format(target=quote + mention + quote)
                        with self.subTest(task=task, path=path):
                            self.assertNotEqual(classify(task, [path]).lane, "fast")

    def test_unicode_disguises_and_quoted_path_syntax(self):
        tricks = ("\u200b", "\u200d", "\u202e", "\u2066", "\ufeff", "\u034f", "\u0307",
                  "\u00ad", "\u00a0", "\u2009", "\t", "\n", "\x08")
        for target_class, _, template in FORMS:
            for path, task in tasks(target_class, template):
                # Exercise characters in the action, between words and at EOF.
                for trick in tricks:
                    for disguised in (trick + task, task[:1] + trick + task[1:], task + trick,
                                      task.replace(" ", trick, 1)):
                        with self.subTest(task=disguised):
                            self.assertNotEqual(classify(disguised, [path]).lane, "fast")
                fullwidth = "".join(chr(ord(c) + 0xfee0) if "!" <= c <= "~" else c
                                    for c in task)
                self.assertNotEqual(classify(fullwidth, [path]).lane, "fast")
                # Kelvin sign has a canonical (not compatibility) decomposition.
                disguised = task.replace("ekle", "eKle").replace("örnek", "örneK")
                if disguised != task:
                    self.assertNotEqual(classify(disguised, [path]).lane, "fast")
            extension = ".md" if target_class == "doc" else ".py"
            for name in ("аlpha", "αlpha", "αλφα", "ａｌｐｈａ", "K", "a\u200bb", "a;true",
                         "$(true)", "a|b", "a>b", "a\\b", "a\nb"):
                path = "docs/" + name + extension
                task = template.format(target="`" + path + "`")
                with self.subTest(task=task):
                    self.assertNotEqual(classify(task, [path]).lane, "fast")

    def test_every_unicode_format_character_stays_off_fast(self):
        format_chars = tuple(chr(n) for n in range(sys.maxunicode + 1)
                             if unicodedata.category(chr(n)) == "Cf")
        for target_class, _, template in FORMS:
            path, task = next(tasks(target_class, template))
            for char in format_chars:
                with self.subTest(template=template, character=ord(char)):
                    self.assertNotEqual(classify(task + char, [path]).lane, "fast")

    def test_outside_unresolved_ambiguous_and_malformed_targets(self):
        for target_class, _, template in FORMS:
            path = "docs/guide.md" if target_class == "doc" else "src/widget.py"
            basename = path.rsplit("/", 1)[-1]
            for mention in ("/tmp/" + basename, "../" + basename, "~/" + basename,
                            "elsewhere/" + basename, "https://example.test/" + basename,
                            "docs//" + basename, "missing.md", "`" + path, "``" + path + "``"):
                for quote in ("", "`"):
                    task = template.format(target=quote + mention + quote)
                    with self.subTest(task=task):
                        self.assertNotEqual(classify(task, [path]).lane, "fast")
            for mention in (basename, "`" + basename + "`"):
                result = classify(template.format(target=mention), [path, "other/" + basename])
                self.assertNotEqual(result.lane, "fast")

    def test_risk_precedence_project_additions_and_length_limits(self):
        for target_class, _, template in FORMS:
            for path, task in tasks(target_class, template):
                for word in ("auth", "security", "push", "commit", "send", "delete", "sil"):
                    result = classify(task + " " + word, [path])
                    self.assertEqual(result.lane, "standard" if word in ("delete", "sil") else "risky")
                for risk_map in (RiskMap(risky_paths=(path,)), RiskMap(risky_words=("guide", "widget"))):
                    self.assertEqual(classify(task, [path], risk_map).lane, "risky")
                for size in (200, 201):
                    result = classify(task.ljust(size), [path])
                    self.assertEqual(result.lane, "fast" if size == 200 else "standard")
                result = classify(task + " a" * 26, [path])
                self.assertNotEqual(result.lane, "fast")
                self.assertIn("task_too_long_for_fast", {s.id for s in result.signals})
                for files in (None, []):
                    self.assertNotEqual(classify(task, files).lane, "fast")


if __name__ == "__main__":
    unittest.main()
