"""T58 lane.v2: goal + done + notes, the lane only rises (T57 r2 §1.2, decision 027 Ş1)."""

import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from tests.unit.test_gitutil import GitFixture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe import classify as classifier
from orcexe.classify import (CLASSIFIER_VERSION, Classification, RiskMap, classify,
                             classify_repo, task_lane, task_lane_repo)
from tests.unit import test_classify as v1_tests

LANES = ("fast", "standard", "risky")
TRACKED = ["scripts/git_push.py", "scripts/publish.sh", "tools/publish.py",
           "src/orcexe/published.py", "src/orcexe/gitutil.py", "src/orcexe/task.py",
           "src/orcexe/do.py", "src/orcexe/run.py", "README.md", "docs/guide.md",
           "src/exitcodes.py", "tests/security/test_redact.py", "src/wipe_cache.py"]
NEUTRAL_GOAL = "Fix the typo in README.md"


def lane(goal, done=(), notes="", files=TRACKED):
    return task_lane(goal, list(done), notes, files).lane


def field_ids(result, field):
    return {s.id.split(":", 1)[1] for s in result.signals if s.id.startswith(field + ":")}


class ApiTests(unittest.TestCase):
    def test_version_and_shape(self):
        self.assertRegex(CLASSIFIER_VERSION, r"^lane\.v[1-9][0-9]*$")
        self.assertEqual(CLASSIFIER_VERSION, "lane.v2")
        result = task_lane(NEUTRAL_GOAL, ["The spelling is corrected."], "", TRACKED)
        self.assertIsInstance(result, Classification)
        self.assertEqual(result.lane, "fast")
        self.assertEqual(task_lane(NEUTRAL_GOAL, [], None, None).lane,
                         classify(NEUTRAL_GOAL, None).lane)

    def test_wrong_types(self):
        for args in [(1, [], "", None), ("x", "done", "", None), ("x", [1], "", None),
                     ("x", [], 3, None), ("x", None, "", None)]:
            with self.subTest(args=args), self.assertRaises(TypeError):
                task_lane(*args)

    def test_signals_name_their_field_and_any_position_counts(self):
        done = ["The spelling is corrected."] * 3 + ["The branch is pushed to origin."]
        for position in range(4):
            items = done[-position:] + done[:-position] if position else done
            result = task_lane(NEUTRAL_GOAL, items, "", TRACKED)
            index = items.index(done[-1])
            self.assertEqual(result.lane, "risky")
            self.assertIn("done[{}]:A8".format(index), {s.id for s in result.signals})
        result = task_lane(NEUTRAL_GOAL, [], "Cleanup uses shutil.rmtree(path).", TRACKED)
        self.assertEqual(result.lane, "risky")
        self.assertIn("notes:A2", {s.id for s in result.signals})
        result = task_lane("Call git.push() afterwards", [], "", TRACKED)
        self.assertIn("goal:A2", {s.id for s in result.signals})

    def test_details_never_carry_task_text(self):
        secret = "hunter2-sk-live-abcdef"
        result = task_lane(NEUTRAL_GOAL, ["`curl -H " + secret + " x`"], secret + " password", TRACKED)
        self.assertEqual(result.lane, "risky")
        self.assertFalse(any(secret in s.detail for s in result.signals))


class ActionRuleTests(unittest.TestCase):
    """Each A-rule in done/notes (and goal), with twins that must stay quiet."""

    RULES = {
        "A1": ["`git add . && git commit && git push`", "`cd .. && rm -rf *`",
               "`rsync -a out/ deploy@host:/srv`", "`find . -name '*.pyc' -delete`",
               "```\npsql -c 'drop table users'\n```", "`curl -X POST https://example.invalid/a. b`",
               "``twine upload dist/*``"],
        "A2": ["git.push()", "requests.put(url, data)", "os.remove(path)", "os.unlink(path)",
               "smtplib.SMTP(host).sendmail(a, b, c)", "boto3.client('s3').upload_file(f, b, k)",
               "socket.create_connection((host, 22))", "urlopen(url)", "x.delete(id)",
               "subprocess.run(['git', 'push'])", "os.system(\"curl https://example.invalid\")"],
        "A3": ["Use curl to fetch it", "The script calls ssh into the box", "uses gh for the release",
               "force-push the branch", "force push is used", "docker push happens", "npm publish runs",
               "See curl(1) for details", "git push is required"],
        "A4": ["Push the branch", "Finally, push", "Please publish the package", "- tag v1.0",
               "Then merge it.", "Fix X and also send the summary", "1. commit everything"],
        "A4-TR": ["Dalı origin'e gönder", "yayınla ve README'de paylaş", "Dalları birleştir.",
                  "Paket PyPI'ye yüklendi.", "Sonuçları pushla sonra kontrol et"],
        "A5": ["Run scripts/git_push.py", "The helper runs scripts/git_push.py",
               "launch tools/publish.py", "scripts/git_push.py dosyasını çalıştır",
               "Execute the deploy step", "Start the merging job"],
        "A6": ["Use rmtree on the build dir", "Erase the cache", "rm the file", "Do a reset --hard",
               "run git clean afterwards", "Drop table users", "delete from logs where x",
               "Geçici dosyaları yok et", "Wipe the folder"],
        "A7": ["Issue a DELETE request", "It sends POST data", "Use PUT for updates", "PATCH the record"],
        "A8": ["The branch is pushed to origin", "It pushes the branch and opens a PR",
               "The wheel is uploaded to PyPI", "Summary sent by email", "Docs are published to the site",
               "Deploying the release", "Changes merged into main", "The report is emailed to the team"],
    }

    QUIET = ["The spelling is corrected.", "the tables and commits list stay the same",
             "`_tokens` stay private", "tests/security/test_redact.py passes",
             "Refactor scripts/git_push.py for clarity", "published.prepare keeps its signature",
             "Running `python3 tests/tools/run_tests.py` passes", "`git status --short` is empty",
             "Uses the commit message format", "The posting date is shown", "Count commits per author",
             "Rename `table_name` locals", "The README.md table lists options", "Update docs/guide.md",
             "src/wipe_cache.py keeps working", "It should show the pushed state",
             "Share the posting date"]

    def test_each_rule_catches_in_done_and_notes(self):
        for rule, texts in self.RULES.items():
            self.assertGreaterEqual(len(texts), 3, rule)
            for text in texts:
                with self.subTest(rule=rule, text=text):
                    done = task_lane(NEUTRAL_GOAL, [text], "", TRACKED)
                    self.assertEqual(done.lane, "risky")
                    self.assertIn(rule, field_ids(done, "done[0]"))
                    notes = task_lane(NEUTRAL_GOAL, [], text, TRACKED)
                    self.assertEqual(notes.lane, "risky")
                    self.assertIn(rule, field_ids(notes, "notes"))

    def test_rules_also_apply_to_goal(self):
        """K-0.4: a text risky in done/notes stays risky as the goal."""
        for texts in self.RULES.values():
            for text in texts:
                with self.subTest(text=text):
                    self.assertEqual(lane(text), "risky")

    def test_twins_stay_quiet_in_done_and_notes(self):
        for text in self.QUIET:
            with self.subTest(text=text):
                self.assertEqual(lane(NEUTRAL_GOAL, [text]), "fast")
                self.assertEqual(lane(NEUTRAL_GOAL, [], text), "fast")
                risky = {s.id for s in task_lane(NEUTRAL_GOAL, [text], text, TRACKED).signals
                         if s.id.split(":", 1)[-1] in classifier._FIELD_RISKY}
                self.assertEqual(risky, set())

    def test_rule_twins(self):
        pairs = [("Push the branch", "The push button is blue"),            # A4 vs noun
                 ("The branch is pushed to origin", "The pushed state is shown"),  # A8 needs a target
                 ("`git push origin main`", "`git status`"),                   # A1
                 ("Run scripts/git_push.py", "Refactor scripts/git_push.py"),   # A5 / hidden path
                 ("Dalı gönder", "Gönderilen dal listesi okunur"),             # A4-TR at the end only
                 ("Issue a DELETE", "Delete is a soft word here"),             # A7 is uppercase only
                 ("Use rmtree", "Use the tree")]
        for risky, quiet in pairs:
            with self.subTest(risky=risky):
                self.assertEqual(lane(NEUTRAL_GOAL, [risky]), "risky")
                self.assertNotEqual(lane(NEUTRAL_GOAL, [quiet]), "risky")

    def test_soft_destructive_floors_standard_and_call_form_is_risky(self):
        for text in ("Remove the stale cache", "Delete old notes", "Eski dosyaları sil",
                     "Geçici klasörü temizle", "Drop the unused flag"):
            with self.subTest(text=text):
                result = task_lane(NEUTRAL_GOAL, [text], "", TRACKED)
                self.assertEqual(result.lane, "standard")
                self.assertIn("destructive", field_ids(result, "done[0]"))
        for text in ("os.remove(p) cleans it", "cache.delete(key)", "items.remove(x) is used"):
            with self.subTest(text=text):
                self.assertEqual(lane(NEUTRAL_GOAL, [text]), "risky")
        self.assertEqual(lane("Remove the unused import"), "standard")


class NarrowRiskWordTests(unittest.TestCase):
    def test_narrow_subset_raises_exact_words_and_plural(self):
        for text in ("Stripe secret key is never logged", "Skip the password check",
                     "credentials stay in the keychain", "The login page works", "Run the migration",
                     "billing totals match", "Şifreler gizli kalır", "Ödeme ekranı açılır",
                     "invoices are listed", "encrypt at rest", "No PII is printed",
                     "GDPR export works", "KVKK metni güncel", "parola alanı", "faturalar"):
            with self.subTest(text=text):
                result = task_lane(NEUTRAL_GOAL, [], text, TRACKED)
                self.assertEqual(result.lane, "risky")
                self.assertIn("K4", field_ids(result, "notes"))

    def test_other_risk_words_are_information_only(self):
        for text in ("tests/security covers it", "`_tokens` helper", "The product list",
                     "Release notes are clear", "env names unchanged", "token counts are logged",
                     "customer facing copy", "authority of the doc"):
            with self.subTest(text=text):
                result = task_lane(NEUTRAL_GOAL, [], text, TRACKED)
                self.assertEqual(result.lane, "fast")
        info = task_lane(NEUTRAL_GOAL, [], "token counts are logged", TRACKED)
        self.assertIn("notes:risk_word", {s.id for s in info.signals})

    def test_narrow_words_inside_identifiers_or_backticks_do_not_raise(self):
        for text in ("`password` field name", "src/auth_helpers.py untouched", "check_password stays",
                     "userAuth is renamed"):
            with self.subTest(text=text):
                self.assertEqual(lane(NEUTRAL_GOAL, [text]), "fast")

    def test_project_risk_words_raise_in_done_and_notes(self):
        result = task_lane(NEUTRAL_GOAL, ["The ledger totals match"], "", TRACKED, RiskMap(("ledger",)))
        self.assertEqual(result.lane, "risky")


class GoalHidingTests(unittest.TestCase):
    """K-2: identifiers hide stems only when resolved, tracked and not called."""

    P2_4_NINE = ["Fix scripts/publish.sh", "Make git.push() retry", "Wrap subprocess.run(['git', 'push'])",
                 "Replace os.system(\"curl https://example.invalid\")", "Explain curl(1) flags",
                 "Squash the commit(s)", "Clean up with rmtree", "Fix kubectl.apply usage", "Use rm -rf there"]
    CALL_FORMS = ["Run the scripts/git_push.py helper", "The helper runs scripts/git_push.py",
                  "Start scripts/git_push.py nightly", "launch tools/publish.py", "python3 -m tools.publish",
                  "scripts/git_push.py dosyasını çalıştır", "Use `publish`"]
    DOTTED = ["Update do.push", "Update run.publish_all", "Fix task.commit", "Fix gitutil.push", "Fix git.push"]
    LEGIT = [("Refactor scripts/git_push.py", "standard"), ("Tidy published.prepare", "standard"),
             ("Fix the typo in README.md", "fast"), ("Remove the unused import", "standard"),
             ("Add a docstring to src/exitcodes.py", "fast"),
             ("Fix scripts/git_push.py:12-30 typo handling", "standard")]

    def test_p2_4_nine_call_forms_and_dotted_names(self):
        for text in self.P2_4_NINE + self.CALL_FORMS + self.DOTTED:
            with self.subTest(text=text):
                self.assertEqual(lane(text), "risky")

    def test_legit_hidden_twins(self):
        for text, expected in self.LEGIT:
            with self.subTest(text=text):
                self.assertEqual(lane(text), expected)

    def test_bare_stem_and_untracked_paths_do_not_hide(self):
        self.assertEqual(lane("Refactor scripts/git_push.py", files=[]), "risky")
        self.assertEqual(lane("Tidy published.prepare", files=["src/other.py"]), "risky")
        # A bare file stem does not hide (publish -> publish.py).
        self.assertEqual(lane("Tidy `publish` docs", files=["publish.py"]), "risky")
        self.assertEqual(lane("Tidy publish.py docs", files=["publish.py"]), "standard")

    def test_call_context_is_sentence_wide_both_directions(self):
        self.assertEqual(lane("Refactor scripts/git_push.py. Run the tests."), "standard")
        self.assertEqual(lane("Refactor scripts/git_push.py and run the tests."), "risky")
        self.assertEqual(lane("Run the tests after fixing scripts/git_push.py"), "risky")  # known cost

    def test_f10_nouns(self):
        self.assertEqual(lane("Count commits whose subject starts with fix"), "risky")
        self.assertEqual(lane("Count Turkish fix keywords in history fix_commits"), "risky")
        for text in ("List the tags", "Show posts", "Count emails"):
            with self.subTest(text=text):
                self.assertEqual(lane(text), "risky")
        # F10 nouns in done/notes stay quiet (only action shapes raise there).
        for text in ("Commits are counted per author", "tags are listed", "posts render"):
            with self.subTest(text=text):
                self.assertEqual(lane(NEUTRAL_GOAL, [text]), "fast")

    def test_uppercase_inflections_and_turkish_stay_caught(self):
        for text in ("POSTs the data", "PUSHes the branch", "SENDs reports", "COMMITs often",
                     "Dalı gönder", "Paketi yayınla", "Değişiklikleri birleştir", "Dosyayı yükle"):
            with self.subTest(text=text):
                self.assertEqual(lane(text), "risky")
                self.assertEqual(classify(text, TRACKED).lane, "risky")
        for text in ("It PUSHes the branch to origin", "The report is EMAILED to the team"):
            with self.subTest(text=text):
                self.assertEqual(lane(NEUTRAL_GOAL, [text]), "risky")

    def test_backtick_rules(self):
        self.assertEqual(lane(NEUTRAL_GOAL, ["`git push`"]), "risky")       # spaced: command
        self.assertEqual(lane(NEUTRAL_GOAL, ["`git_push`"]), "risky")       # A3 parts
        self.assertEqual(lane(NEUTRAL_GOAL, ["`README.md` is clear"]), "fast")
        # An odd backtick leaves the field as prose.
        self.assertEqual(lane(NEUTRAL_GOAL, ["Then push ` the branch"]), "risky")
        # Fields never pair backticks across each other.
        result = task_lane(NEUTRAL_GOAL, ["Mention `git", "push` later"], "", TRACKED)
        self.assertEqual(result.lane, "risky")
        self.assertEqual(lane(NEUTRAL_GOAL, ["Mention `git"]), "fast")
        # Fences are command spans even with '. ' inside.
        self.assertEqual(lane(NEUTRAL_GOAL, [], "```\necho done. git commit -m x\n```"), "risky")

    def test_word_plural_marks_are_not_calls(self):
        self.assertEqual(lane("Squash commit(s)"), "risky")
        self.assertEqual(lane("Update the file(s) list"), "standard")
        self.assertEqual(lane(NEUTRAL_GOAL, ["See curl(1)"]), "risky")


class DifferentialTests(unittest.TestCase):
    """lane_v2 >= lane_v1 for every goal text in today's tests (K-0.1, K25 a)."""

    def goals(self):
        texts = {case[0] for case in v1_tests.CASES}
        for name in dir(v1_tests):
            value = getattr(v1_tests, name)
            if isinstance(value, (list, tuple)):
                for item in value:
                    if isinstance(item, str):
                        texts.add(item)
                    elif isinstance(item, tuple) and item and isinstance(item[0], str):
                        texts.add(item[0])
        texts.update(GoalHidingTests.P2_4_NINE + GoalHidingTests.CALL_FORMS + GoalHidingTests.DOTTED)
        texts.update(t for texts in ActionRuleTests.RULES.values() for t in texts)
        texts.update(ActionRuleTests.QUIET)
        return sorted(texts)

    def test_never_lower_than_v1(self):
        goals = self.goals()
        self.assertGreater(len(goals), 100)
        for files in (None, [], v1_tests.TREE, TRACKED):
            for goal in goals:
                v1 = classify(goal, files).lane
                v2 = task_lane(goal, [], "", files).lane
                self.assertGreaterEqual(LANES.index(v2), LANES.index(v1), (goal, files))
                # Done/notes can only raise.
                v2b = task_lane(goal, ["The spelling is corrected."], "Notes.", files).lane
                self.assertGreaterEqual(LANES.index(v2b), LANES.index(v1), (goal, files))

    def test_fast_comes_only_from_the_goal(self):
        for goal in self.goals():
            if task_lane(goal, [], "", TRACKED).lane != "fast":
                self.assertNotEqual(task_lane(goal, ["Add a docstring to src/exitcodes.py"],
                                              "Fix the typo in README.md", TRACKED).lane, "fast")


class RiskMapFloorTests(GitFixture):
    """P1-3: an unusable risk map never lowers a text lane and never yields fast."""

    BROKEN = ["version: [\n", "version: 99\n", "[]\n", "version: 1\nunknown: []\n",
              "version: 1\nrisky_words: foo\n", "version: 1\nrisky_words: [1]\n", '"text"\n']

    def setUp(self):
        super().setUp()
        (self.main / "README.md").write_text("example", encoding="utf-8")
        self.git_run(self.main, "add", ".")
        self.path = self.main / ".orcexe" / "risk-map.yaml"
        self.path.parent.mkdir()

    def check(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            for goal, done, expected in [("auth README.md", [], "risky"),
                                         ("Push README.md upstream", [], "risky"),
                                         (NEUTRAL_GOAL, ["The branch is pushed to origin"], "risky"),
                                         (NEUTRAL_GOAL, [], "standard"),
                                         ("Fix README.md", [], "standard")]:
                result = task_lane_repo(str(self.main), goal, done, "")
                self.assertEqual(result.lane, expected, goal)
                self.assertIn("risk_map_invalid", {s.id for s in result.signals})
                if not done:
                    old = classify_repo(str(self.main), goal)
                    self.assertEqual(old.lane, expected, goal)

    def test_corrupt_wrong_type_unknown_key_and_version(self):
        for text in self.BROKEN:
            with self.subTest(text=text):
                self.path.write_text(text, encoding="utf-8")
                self.check()

    def test_dangling_symlink_and_unreadable(self):
        self.path.symlink_to("absent.yaml")
        self.check()
        self.path.unlink()
        self.path.write_text("version: 1\n", encoding="utf-8")
        for error in (PermissionError("denied"), TypeError("odd"), RecursionError("deep")):
            with self.subTest(error=error), mock.patch.object(classifier, "load_risk_map", side_effect=error):
                self.check()

    def test_valid_map_and_no_map(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(task_lane_repo(str(self.main), NEUTRAL_GOAL, [], "").lane, "fast")
            self.path.write_text("version: 1\nrisky_words: [ledger]\n", encoding="utf-8")
            self.assertEqual(task_lane_repo(str(self.main), NEUTRAL_GOAL, ["ledger is balanced"], "").lane,
                             "risky")
            self.assertEqual(task_lane_repo(str(self.home), "Help me", [], "").mode, "everyday")

    def test_risk_areas_floor(self):
        from orcexe import risk_areas
        self.path.write_text("version: 2\n", encoding="utf-8")
        areas = risk_areas.repository_areas(self.main, {})
        invalid = [a for a in areas if a.basis == "cli:risk_map_invalid:unavailable"]
        self.assertTrue(invalid)
        self.assertTrue(all(a.lane in ("standard", "risky") for a in invalid))
        for error in (TypeError("odd"), RecursionError("deep")):
            with self.subTest(error=error), mock.patch.object(classifier, "load_risk_map", side_effect=error):
                bases = {a.basis for a in risk_areas.repository_areas(self.main, {})}
                self.assertIn("cli:risk_map_invalid:unavailable", bases)


if __name__ == "__main__":
    unittest.main()
