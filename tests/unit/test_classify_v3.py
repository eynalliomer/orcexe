"""T68 lane.v3: T58 L2 P2-1, P2-2, P2-4 and decision 028 (risk-map floor).

Catching sentences come from the L2 review's observations; every rule has
false-alarm twins that must not become risky.
"""

import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from tests.unit.test_gitutil import GitFixture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe import classify as classifier
from orcexe.classify import CLASSIFIER_VERSION, classify, classify_repo, task_lane, task_lane_repo

LANES = ("fast", "standard", "risky")
# T68 r6 (rule R8): the full grid, the 300-text random differential and the
# three-file-set loops run only with ORCEXE_SLOW_TESTS=1 (separate CI job). The
# default run keeps a deterministic subset that covers every grid family and
# every exemption name.
SLOW_TESTS = os.environ.get("ORCEXE_SLOW_TESTS") == "1"
GOAL = "Tidy the loader in src/app.py"
NEUTRAL_GOAL = "Fix the typo in README.md"
TRACKED = ["src/app.py", "scripts/git_push.py", "tools/publish.py", "README.md", "src/exitcodes.py"]


def in_fields(text, goal=GOAL, files=TRACKED):
    """(done lane, notes lane) for one text."""
    return (task_lane(goal, [text], "", files).lane, task_lane(goal, [], text, files).lane)


def risky_rules(text, goal=GOAL, files=TRACKED):
    result = task_lane(goal, [text], text, files)
    return {s.id for s in result.signals if s.id.split(":", 1)[-1] in classifier._FIELD_RISKY}


class VersionTests(unittest.TestCase):
    def test_version_is_lane_v3(self):
        self.assertEqual(CLASSIFIER_VERSION, "lane.v3")


class BareVerbTests(unittest.TestCase):
    """P2-1: bare outward verbs mid-sentence, post/tag, ':' starts, Turkish forms."""

    CATCH = [
        "When the tests pass, push the branch to origin.", "Once done, push to origin/main.",
        "Make sure to publish the package to PyPI.", "Remember to deploy the build afterwards.",
        "The worker must push the result to origin.", "The script will upload the wheel to the bucket.",
        "Do not forget to send the report to the team.", "After that, push the branch.",
        "TODO: push the branch to origin.", "Step 3: publish the package.",
        "Build the wheel, upload it to PyPI.", "Then cargo publish the crate.",
        "Use docker compose push for the images.", "The helper POSTs the payload to the API.",
        "The commit is pushed.", "Changes are committed.", "A release is published on PyPI.",
        "The report is sent via Slack.", "The branch is then pushed.",
        "It tags the release and posts the summary to the channel.",
        "Make sure to post the report to Slack.",
    ]
    CATCH_TR = [
        "Değişiklikleri commit et ve push et.", "Dalı origin'e push et.", "Testler geçince deploy et.",
        "Ana dala merge et.", "Paketi PyPI'ye yükleyip sürümü duyur.", "Dalı origin'e gönderin lütfen.",
        "Sürümü yayına al.", "Canlıya al.", "Değişiklikler commit edilir.", "Bir commit at.",
        "Raporu ekibe gönderip kapat.", "Paketi yayınlayın lütfen.",
    ]
    # Already caught by lane.v2; must stay caught.
    STILL = ["Sonunda dalı pushla.", "Paketi yayınla ve paylaş.", "Raporu ekibe e-posta ile gönder."]
    QUIET = [
        "Do not push the branch to origin.", "Never push to main.", "Make sure not to push the branch.",
        "Nothing is pushed.", "No branch is pushed.", "The branch is not pushed.",
        "No plan.json or plan.md is published and existing .orcexe state bytes are unchanged",
        "Fix the commit message to include the id.", "Link the commit to the issue.",
        "Add a tag to the changelog entry.", "The push to origin is documented.",
        "Note: tag names are kept.", "Install the post-merge hook.",
        "The posting date is shown.",
        "tags are listed", "posts render", "The dicts are merged.", "The issue is tagged as a bug.",
        "Commits are counted per author.", "Raporda push etiketi görünür.",
    ]

    def test_observations_are_risky_in_done_and_notes(self):
        for text in self.CATCH + self.CATCH_TR + self.STILL:
            with self.subTest(text=text):
                self.assertEqual(in_fields(text), ("risky", "risky"))

    def test_rule_ids(self):
        self.assertIn("notes:A8", risky_rules("When the tests pass, push the branch to origin."))
        self.assertIn("notes:A8", risky_rules("The commit is pushed."))
        self.assertIn("notes:A8", risky_rules("The helper POSTs the payload to the API."))
        self.assertIn("notes:A4", risky_rules("TODO: push the branch to origin."))
        self.assertIn("notes:A3", risky_rules("Then cargo publish the crate."))
        self.assertIn("notes:A3", risky_rules("Use docker compose push for the images."))
        for text in ("Dalı origin'e push et.", "Paketi PyPI'ye yükleyip sürümü duyur.",
                     "Dalı origin'e gönderin lütfen.", "Canlıya al."):
            with self.subTest(text=text):
                self.assertIn("notes:A4-TR", risky_rules(text))

    def test_twins_stay_below_risky(self):
        for text in self.QUIET:
            with self.subTest(text=text):
                self.assertNotIn("risky", in_fields(text, NEUTRAL_GOAL))
                self.assertEqual(risky_rules(text, NEUTRAL_GOAL), set())

    def test_kebab_parts_keep_the_v2_run_context_catch(self):
        # A kebab word is excluded from the new bare-verb rule, never from lane.v2's A5.
        self.assertEqual(in_fields("Install the post-merge hook to run checks.", NEUTRAL_GOAL),
                         ("risky", "risky"))
        self.assertIn("notes:A5", risky_rules("Install the post-merge hook to run checks.", NEUTRAL_GOAL))

    def test_turkish_at_needs_the_sentence_end(self):
        self.assertEqual(in_fields("Make one commit at the end.", NEUTRAL_GOAL), ("fast", "fast"))
        self.assertEqual(in_fields("Değişiklikleri commit at ve push et.")[0], "risky")

    def test_goal_never_below_v1(self):
        for text in self.CATCH + self.CATCH_TR + self.STILL + self.QUIET:
            for files in (None, [], TRACKED):
                with self.subTest(text=text, files=files):
                    v1 = classify(text, files).lane
                    v3 = task_lane(text, [], "", files).lane
                    self.assertGreaterEqual(LANES.index(v3), LANES.index(v1))
                    if text in self.CATCH + self.CATCH_TR + self.STILL:
                        self.assertEqual(v3, "risky")


class PartsOnlyTests(unittest.TestCase):
    """P2-2: A5 searches identifier/command parts; mid-sentence A4 needs an object."""

    QUIET = [
        "Keep the pre and post conditions unchanged.", "Branch and tag names stay the same.",
        "The name and email fields are validated.", "The source of truth stays the committed fixture.",
        "The output is truncated when the suite starts.", "It starts with the tagline and the postal code.",
        "Koşul sağlanırsa etiketler birleştirilir.", "Paylaşılır bir not.",
        "Tag names in the parser are case-insensitive.", "Commit messages are validated.",
        # Real T47 done items (plan3 T2, plan4 T1) that lane.v2 made risky through A5.
        "No manager model call is made, and nothing is created under the configured state "
        "directory other than the published .orcexe outputs.",
        "makes no model call and publishes no plan.json",
        "The sourced values stay the same.",
    ]
    CATCH = [
        ("Run scripts/git_push.py", "A5"), ("The helper runs scripts/git_push.py", "A5"),
        ("Execute the deploy step", "A5"), ("Start the merging job", "A5"),
        ("Run the publish script", "A5"), ("Trigger a deploy", "A5"),
        ("source scripts/git_push.py first", "A5"), ("Call `git_push` afterwards", "A5"),
        ("Fix X and also send the summary", "A4"), ("Fix it and push.", "A4"),
        ("Fix it and then push it.", "A4"), ("Fix it, and push `main`.", "A4"),
        ("Dalları birleştir.", "A4-TR"), ("Paket PyPI'ye yüklendi.", "A4-TR"),
        ("Rapor ekibe gönderilir.", "A4-TR"),
    ]

    def test_false_alarm_twins_are_not_risky(self):
        for text in self.QUIET:
            with self.subTest(text=text):
                self.assertNotIn("risky", in_fields(text, NEUTRAL_GOAL))
                self.assertEqual(risky_rules(text, NEUTRAL_GOAL), set())

    def test_catches_keep_their_rule(self):
        for text, rule in self.CATCH:
            with self.subTest(text=text):
                self.assertEqual(in_fields(text), ("risky", "risky"))
                self.assertIn("notes:" + rule, risky_rules(text))

    def test_prose_outward_word_counts_only_as_the_run_object(self):
        self.assertIn("notes:A5", risky_rules("Run the publish step."))
        # T68 r4: lane.v2's A5 prefix match over prose is the base; no exemption names these.
        for text in ("Run the tests and the published docs stay unchanged.",
                     "Run the publisher tests and note the pushback."):
            with self.subTest(text=text):
                self.assertIn("notes:A5", risky_rules(text, NEUTRAL_GOAL))


class LineBreakTests(unittest.TestCase):
    """P2-4: one line break is a space; abbreviations do not end a sentence."""

    CATCH = [
        "After the build, run\nscripts/git_push.py to finish.",
        "Run e.g. scripts/git_push.py after the build.",
        "The branch is pushed\nto origin once tests pass.",
        "Execute (i.e. tools/publish.py) after tests.",
        "Run the checks, vs. the old flow, then\nscripts/git_push.py runs.",
        "Run lint, tests etc. and\nscripts/git_push.py at the end.",
        "Upload the\nwheel to PyPI.",
    ]
    QUIET = [
        "Refactor scripts/git_push.py\n\nrun the tests afterwards",
        "Refactor scripts/git_push.py\n- run the tests afterwards",
        "Refactor scripts/git_push.py\n2. run the tests afterwards",
        "Refactor scripts/git_push.py. Run the tests.",
    ]

    def test_wrapped_and_abbreviated_sentences_are_caught(self):
        for text in self.CATCH:
            with self.subTest(text=text):
                self.assertEqual(in_fields(text), ("risky", "risky"))

    def test_blank_lines_bullets_and_full_stops_still_split(self):
        for text in self.QUIET:
            with self.subTest(text=text):
                self.assertNotIn("risky", in_fields(text, NEUTRAL_GOAL))

    def test_sentence_boundaries(self):
        def split(text):
            return [" ".join(t for _, t in s) for s in classifier._sentences(text)]
        self.assertEqual(split("a b\nc d"), ["a b c d"])
        self.assertEqual(split("a b\n\nc d"), ["a b", "c d"])
        self.assertEqual(split("a b\n  \nc d"), ["a b", "c d"])
        self.assertEqual(split("intro\n- one\n* two\n1. three"), ["intro", "one", "two", "three"])
        self.assertEqual(split("Use e.g. this, i.e. that, vs. those etc. now. Next"),
                         ["Use e.g. this, i.e. that, vs. those etc. now.", "Next"])
        # T68 r4 (P1-3): an abbreviation or one letter + dot before a capital ends a sentence.
        self.assertEqual(split("Ask J. Doe (E.G. later). Done"), ["Ask J.", "Doe (E.G. later).", "Done"])
        self.assertEqual(split("Update docs, tests, etc. Deploy the site."),
                         ["Update docs, tests, etc.", "Deploy the site."])
        self.assertEqual(split("Run e.g. scripts/x.py now"), ["Run e.g. scripts/x.py now"])
        self.assertEqual(split("Plan A.\nPush it"), ["Plan A.", "Push it"])
        self.assertEqual(split("intro\n– one\n— two\n· three\n> four\na. five\nb) six"),
                         ["intro", "one", "two", "three", "four", "five", "six"])
        self.assertEqual(split("One; two! three? four."), ["One;", "two!", "three?", "four."])
        self.assertEqual(split("run\n`x y`\nnext"), ["run x y next"])


class RiskMapRiskyFloorTests(GitFixture):
    """Decision 028: a present but unusable risk map floors both repo entry points at risky."""

    BROKEN = [b"version: [\n", b"version: 2\n", b"", b"\n", b"\x00\xff\xfe\x01binary",
              b"[]\n", b"version: 1\nunknown: []\n", b"version: 1\nrisky_words: [1]\n", b'"text"\n']
    HARMLESS = [(NEUTRAL_GOAL, [], ""), ("Fix README.md", ["The spelling is corrected."], "")]

    def setUp(self):
        super().setUp()
        (self.main / "README.md").write_text("example", encoding="utf-8")
        self.git_run(self.main, "add", ".")
        self.path = self.main / ".orcexe" / "risk-map.yaml"
        self.path.parent.mkdir()

    def assert_floor(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            for goal, done, notes in self.HARMLESS:
                result = task_lane_repo(str(self.main), goal, done, notes)
                self.assertEqual(result.lane, "risky", goal)
                self.assertIn(classifier.Signal("risk_map_invalid", "unavailable"), result.signals)
                old = classify_repo(str(self.main), goal)
                self.assertEqual(old.lane, "risky", goal)
                self.assertIn(classifier.Signal("risk_map_invalid", "unavailable"), old.signals)

    def test_broken_wrong_version_empty_and_binary(self):
        for raw in self.BROKEN:
            with self.subTest(raw=raw):
                self.path.write_bytes(raw)
                self.assert_floor()

    def test_symlinks_and_unreadable(self):
        target = self.main / "valid-map.yaml"
        target.write_text("version: 1\n", encoding="utf-8")
        self.path.symlink_to(target)
        self.assert_floor()
        self.path.unlink()
        self.path.symlink_to("absent.yaml")
        self.assert_floor()
        self.path.unlink()
        self.path.write_text("version: 1\n", encoding="utf-8")
        for error in (PermissionError("denied"), IsADirectoryError("dir"), RecursionError("deep")):
            with self.subTest(error=error), mock.patch.object(classifier, "load_risk_map", side_effect=error):
                self.assert_floor()

    def test_no_map_and_valid_map_are_unchanged(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(task_lane_repo(str(self.main), NEUTRAL_GOAL, [], "").lane, "fast")
            self.assertEqual(classify_repo(str(self.main), NEUTRAL_GOAL).lane, "fast")
            self.path.write_text("version: 1\nrisky_words: [ledger]\n", encoding="utf-8")
            self.assertEqual(task_lane_repo(str(self.main), NEUTRAL_GOAL, [], "").lane, "fast")
            self.assertEqual(classify_repo(str(self.main), NEUTRAL_GOAL).lane, "fast")
            result = task_lane_repo(str(self.main), NEUTRAL_GOAL, [], "")
            self.assertNotIn("risk_map_invalid", {s.id for s in result.signals})

    def test_git_unavailable_floor_is_still_standard(self):
        from orcexe import gitutil
        self.path.write_text("version: 2\n", encoding="utf-8")
        with mock.patch.dict(os.environ, self.env, clear=True), \
                mock.patch.object(gitutil, "ls_files", side_effect=gitutil.GitError("failed")):
            for result in (task_lane_repo(str(self.main), NEUTRAL_GOAL, [], ""),
                           classify_repo(str(self.main), NEUTRAL_GOAL)):
                self.assertEqual(result.lane, "standard")
                self.assertIn("git_unavailable", {s.id for s in result.signals})
                self.assertNotIn("risk_map_invalid", {s.id for s in result.signals})

    def test_plan_lane_and_risk_area(self):
        from orcexe import planning, risk_areas
        self.path.write_text("version: 2\n", encoding="utf-8")
        task = dict(id="T1", goal=NEUTRAL_GOAL, done=["The spelling is corrected."], notes="",
                    files=dict(allow=["README.md"]), lane="fast")
        with mock.patch.dict(os.environ, self.env, clear=True):
            lane, basis = planning.task_lane(task, {}, self.main)
            areas = risk_areas.repository_areas(self.main, {})
        self.assertEqual(lane, "risky")
        self.assertIn("cli:risk_map_invalid:unavailable", basis)
        invalid = [a for a in areas if a.basis == "cli:risk_map_invalid:unavailable"]
        self.assertEqual([a.lane for a in invalid], ["risky"])


class R2RegressionTests(unittest.TestCase):
    """T68 r2: exemptions may only stop new v3 signals, never a lane.v2 catch."""

    def test_reported_v2_catches_stay_risky_in_every_field(self):
        cases = [("Trigger the mirror-push job when tests pass.", "A5"),
                 ("Özet ekip kanalında paylaşılır.", "A4-TR"),
                 ("Paket PyPI'ye yüklenir.", "A4-TR")]
        for text, rule in cases:
            with self.subTest(text=text):
                self.assertEqual(lane_v3(text), "risky")
                self.assertEqual(in_fields(text, NEUTRAL_GOAL), ("risky", "risky"))
                self.assertIn("notes:" + rule, risky_rules(text, NEUTRAL_GOAL))

    def test_turkish_passive_aorist_exempt_only_without_destination(self):
        for text in ("Ayarlar açılışta yüklenir.", "Paket `main`'e yüklenir.", "Dal sunucuya yüklenir.",
                     "Rapor ekibe paylaşılır.", "Değişiklikler ana dalda birleştirilir.",
                     "Paket yüklenir ve wheel artifact saklanır.",
                     # T68 r4: no conditional clause, so the lane.v2 catch stays.
                     "Etiketler birleştirilir."):
            with self.subTest(text=text):
                self.assertEqual(in_fields(text, NEUTRAL_GOAL), ("risky", "risky"))
        for text in ("Koşul sağlanırsa etiketler birleştirilir.", "Paylaşılır bir not."):
            with self.subTest(text=text):
                self.assertNotIn("risky", in_fields(text, NEUTRAL_GOAL))

    def test_descriptive_start_verb_versus_imperative(self):
        for text in ("Tag names in the parser are case-insensitive.", "Commit messages are validated.",
                     "Push notifications stay disabled.", "Merge conflicts are reported."):
            with self.subTest(text=text):
                self.assertNotIn("risky", in_fields(text, NEUTRAL_GOAL))
        for text in ("Push changes to origin.", "Tag the release.", "Push branches that are ready.",
                     "Tag names, then commit.", "Push it.", "Tag v1.0 and push."):
            with self.subTest(text=text):
                self.assertEqual(in_fields(text, NEUTRAL_GOAL), ("risky", "risky"))


class R3SubordinateClauseTests(unittest.TestCase):
    """T68 r3: a finite verb inside a subordinate clause never makes a start verb descriptive."""

    IMPERATIVE = [
        "Push tags once the release is cut.", "Upload artifacts as soon as tests are green.",
        "Tag releases before the changelog is final.", "Deploy builds if the checks are green.",
        "Send reports because managers are waiting.", "Commit changes while the tree is clean.",
        "Push branches until all checks are green.", "Publish packages unless the version is a draft.",
        "Merge branches so that history stays linear.", "Deploy services then the smoke tests are run.",
        "Upload wheels when the build has finished.", "Send summaries after reviews are done.",
    ]
    # T68 r4: "Deploy scripts must be idempotent." left this list (a modal breaks the exemption).
    DESCRIPTIVE = [
        "Tag names in the parser are case-insensitive.", "Commit messages are validated.",
        "Post titles stay under 80 characters.",
    ]

    def test_imperative_with_subordinate_clause_is_risky(self):
        self.assertGreaterEqual(len(self.IMPERATIVE), 8)
        for text in self.IMPERATIVE:
            with self.subTest(text=text):
                self.assertEqual(in_fields(text, NEUTRAL_GOAL), ("risky", "risky"))
                self.assertEqual(_frozen_v2().task_lane(NEUTRAL_GOAL, [text], text, TRACKED).lane, "risky")

    def test_descriptive_subject_twins_stay_standard_or_lower(self):
        for text in self.DESCRIPTIVE:
            with self.subTest(text=text):
                self.assertNotIn("risky", in_fields(text, NEUTRAL_GOAL))
                self.assertEqual(task_lane(NEUTRAL_GOAL, [text], text, TRACKED).lane, "fast")


def lane_v3(text):
    return task_lane(text, [], "", TRACKED).lane


def _frozen_v2():
    """The lane.v2 classifier, frozen verbatim at tests/fixtures/classify/lane_v2.py."""
    import importlib.util
    name = "orcexe_lane_v2_frozen"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, ROOT / "tests/fixtures/classify/lane_v2.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def corpus():
    """Every v2/v3 test sentence (today's v1 test texts included)."""
    from tests.unit import test_classify_v2 as v2_tests
    texts = set(v2_tests.DifferentialTests().goals())
    for name in ("CATCH", "CATCH_TR", "STILL", "QUIET"):
        texts.update(getattr(BareVerbTests, name))
    texts.update(PartsOnlyTests.QUIET)
    texts.update(text for text, _ in PartsOnlyTests.CATCH)
    texts.update(LineBreakTests.CATCH + LineBreakTests.QUIET)
    texts.update(R3SubordinateClauseTests.IMPERATIVE + R3SubordinateClauseTests.DESCRIPTIVE)
    texts.update(R4ReviewTests.RISKY + R4ReviewTests.OPTIONAL_P2)
    texts.update(text for text, _ in ExemptionTests.EXEMPT)
    texts.update(ExemptionTests.NEAR_MISSES)
    texts.update(["Trigger the mirror-push job when tests pass.", "Özet ekip kanalında paylaşılır.",
                  "Paket PyPI'ye yüklenir.", "Branch, tag and commit lists stay sorted.",
                  "Deploy scripts must be idempotent.", "Etiketler birleştirilir."])
    return sorted(texts), v2_tests


# --- T68 r4 template grid (Fable T68 L2) -------------------------------------
_HEADS = "names messages notifications conflicts requests hooks keys permissions titles scripts limits fields"


def template_grid():
    """[(text, expected exemption name or None)], about 1,800 generated sentences."""
    out = []
    verbs7 = "push publish deploy commit merge upload send".split()
    tails13 = ["again", "it", "main", "the branch", "artifacts to S3", "now", "later", "to origin",
               "staging", "everything", "changes", "the results", ""]
    # P1-1: 3 patterns x 7 verbs x 13 tails (mid-sentence lead + verb).
    for pattern in ("Fix lint and {v} {t}", "Build it and {v} {t}", "Update the docs, then {v} {t}"):
        for verb in verbs7:
            for tail in tails13:
                out.append((pattern.format(v=verb, t=tail).strip() + ".", None))
    # P1-2: 6 run verbs x 6 objects, plus Turkish verb-final run sentences.
    for run in ("Run", "Execute", "Trigger", "Start", "Call", "Source"):
        for obj in ("the deployment", "make to publish the docs", "the tests before deploying",
                    "the deploy script", "the push job", "scripts/git_push.py"):
            out.append((run + " " + obj + ".", None))
    out += [(t, None) for t in ("Sonra deploy işini tetikle.", "Son olarak push betiğini çalıştır.",
                                "Önce testleri koş, sonra deploy betiğini başlat.")]
    # P1-3: line lists, unrecognised bullets, blank lines.
    items = ("Commit", "Push", "Deploy", "Tag the release", "Merge main", "Publish the package",
             "Upload the wheel")
    for first in ("Update version", "Fix the docs"):
        for item in items:
            for sep in ("\n", "\n– ", "\n— ", "\n· ", "\n> ", "\na. ", "\na) ", "\n- ", "\n\n",
                        "\n1. ", "\n* "):
                out.append((first + sep + item, None))
    out.append(("Update version\nCommit\nPush\nDeploy", None))
    # P1-3: abbreviations and one letter + dot, on one line and wrapped.
    for prefix in ("Update docs, tests, etc.", "Fix lint, i.e. style.", "Check e.g. the logs.",
                   "Plan A.", "Ask J.", "Compare old vs. new."):
        for follow in ("Deploy the site.", "Push the branch.", "Then publish it.",
                       "Commit everything.", "Merge main."):
            out.append((prefix + " " + follow, None))
            out.append((prefix + "\n" + follow, None))
    # P1-4: Turkish passive aorist with ile/üzerinden, destinations and the conditional form.
    exempt_tr = {("Etiketler", "birleştirilir"), ("Ayarlar", "yüklenir")}  # T68 r5: exact pairs
    for subject in ("Rapor", "Paket", "PR", "Özet", "Değişiklikler", "Etiketler", "Ayarlar"):
        for middle in ("", "Slack ile", "Slack üzerinden", "ekibe", "sunucuya", "Koşul sağlanırsa",
                       "Koşul sağlanırsa Slack ile"):
            for verb in ("paylaşılır", "yüklenir", "birleştirilir", "gönderilir", "yayınlanır"):
                if middle.startswith("Koşul"):
                    words = middle.split()
                    text = " ".join(words[:2] + [subject.lower()] + words[2:] + [verb]) + "."
                else:
                    text = " ".join(w for w in (subject, middle, verb) if w) + "."
                name = ("exempt:tr_conditional_description"
                        if middle == "Koşul sağlanırsa" and (subject, verb) in exempt_tr else None)
                out.append((text, name))
    # P1-5: work-step labels and descriptive subjects.
    for verb in "Push Deploy Merge Tag Publish Commit Upload Send".split():
        for noun in ("branch", "staging", "main", "release", "the tag", "notifications", "messages"):
            for tail in ("is done.", "must happen before prod.", "must be done before release.",
                         "is complete.", "stays disabled.", "are validated."):
                name = ("exempt:descriptive_subject" if noun in ("notifications", "messages")
                        and tail in ("stays disabled.", "are validated.") else None)
                out.append(("{} {} {}".format(verb, noun, tail), name))
        for head in _HEADS.split():
            for tail in ("are kept.", "stay the same.", "in the parser are case-insensitive.",
                         "remain sorted.", "must be idempotent.", "have changed.", "are pushed to origin."):
                name = None if tail in ("must be idempotent.", "have changed.") else "exempt:descriptive_subject"
                out.append(("{} {} {}".format(verb, head, tail), name))
    # Every review sentence and every exemption test sentence.
    out += [(t, None) for t in R4ReviewTests.RISKY + R4ReviewTests.OPTIONAL_P2]
    out += list(ExemptionTests.EXEMPT)
    out += [(t, None) for t in ExemptionTests.NEAR_MISSES]
    # Review sentences repeat some grid cells; a repeated sentence must carry the same tag.
    unique = {}
    for text, name in out:
        assert unique.setdefault(text, name) == name, text
    return list(unique.items())


def grid_cases():
    """The full grid with ORCEXE_SLOW_TESTS=1, otherwise every 10th generated cell
    (each family is a contiguous block of at least three cells) plus every review,
    exemption and near-miss sentence."""
    grid = template_grid()
    if SLOW_TESTS:
        return grid
    explicit = ({t for t in R4ReviewTests.RISKY + R4ReviewTests.OPTIONAL_P2}
                | {t for t, _ in ExemptionTests.EXEMPT} | set(ExemptionTests.NEAR_MISSES))
    return [case for i, case in enumerate(grid) if i % 10 == 0 or case[0] in explicit]


class V2BaseTests(unittest.TestCase):
    """T68 r4 (1): lane.v2's field signals are computed bit for bit in production."""

    FROZEN_CONSTANTS = """_A_VERBS _A_TOOLS _A_HARD _A_SOFT _A_CALL_EXTRA _A_NET _A_GIT _RUN_WORDS
    _CALL_EXTENSIONS _A4_LEADS _A4_VERBS _A4_TR_NEXT _A8_TARGETS _NARROW_RISK _NARROW_RISK_PREFIX
    _OUTWARD _DESTRUCTIVE _RISK_WORDS _SENTENCE_END _CLOSERS _EDGE""".split()
    RENAMED = {"_A_PAIRS": "_V2_A_PAIRS", "_RUN_VERBS": "_V2_RUN_VERBS", "_A4_TR": "_V2_A4_TR",
               "_A8_FORMS": "_V2_A8_FORMS", "_OUTWARD_FORMS": "_V2_OUTWARD_FORMS",
               "_A8_NOUNS": "_V2_A8_NOUNS"}

    def test_constants_match_the_frozen_fixture(self):
        v2 = _frozen_v2()
        for name in self.FROZEN_CONSTANTS:
            with self.subTest(name=name):
                self.assertEqual(getattr(classifier, name), getattr(v2, name))
        for old, new in self.RENAMED.items():
            with self.subTest(name=new):
                self.assertEqual(getattr(classifier, new), getattr(v2, old))
        self.assertEqual(classifier._V2_BULLET.pattern, v2._BULLET.pattern)
        for pattern in ("_PLURAL_CALL", "_HTTP", "_CALLS", "_QUOTED", "_FENCE", "_DOUBLE", "_SINGLE"):
            self.assertEqual(getattr(classifier, pattern).pattern, getattr(v2, pattern).pattern)

    def test_v2_sentences_and_signals_equal_the_frozen_fixture(self):
        v2 = _frozen_v2()
        texts, v2_tests = corpus()
        texts = sorted(set(texts) | {t for t, _ in grid_cases()})
        self.assertGreater(len(texts), 1500 if SLOW_TESTS else 450)
        for files in (TRACKED, v2_tests.TRACKED, None) if SLOW_TESTS else (TRACKED, None):
            for text in texts:
                with self.subTest(text=text, files=files):
                    self.assertEqual(classifier._v2_sentences(text), v2._sentences(text))
                    for name, goal in (("goal", True), ("notes", False)):
                        frozen = v2._field_signals(name, text, v2._index(files), v2.RiskMap(), goal)
                        self.assertEqual(
                            {(s.id, s.detail) for s in
                             classifier.lane_v2_field_signals(name, text, files, None, goal)},
                            {(s.id, s.detail) for s in frozen})


    def test_seeded_random_texts_equal_the_frozen_fixture(self):
        """T68 r5 (L2 P3-a): a seeded differential over messy generated notes."""
        import random
        rng = random.Random(6820)
        atoms = ("push publish deploy commit merge rebase tag upload send email mail post run execute "
                 "trigger start source call python sh bash node the a to into onto origin main branch "
                 "release package wheel report changes is are pushed sent then and also please finally "
                 "rm rmtree truncate drop delete sil kaldır yok et gönder yayınla yükle paylaş "
                 "birleştir pushla commitle ve sonra yüklenir paylaşılır password secret migration "
                 "şifre ödeme e.g. i.e. vs. etc. J. TODO: Step 3: names messages are kept").split()
        specials = ["scripts/git_push.py", "tools/publish.py", "src/app.py", "README.md",
                    "`git push origin main`", "`git_push`", "``npm publish``", "requests.put(url)",
                    "os.remove('x')", "x.sendmail('a')", "sendEmail", "userAuth", "POST", "DELETE",
                    "self.commit()", "commit(s)", "force-push", "commit/push", "PyPI'ye", "`main`'e",
                    "İSTANBUL", "ıi", "ＰＵＳＨ", "pu\u200bsh", "é", "naïve", "Ǆ", "–", "—", "·",
                    "(", ")", ",", ";", ":", "!", "?", ".", '"', "'", "*", "_", "`"]
        breaks = [" ", " ", " ", "\n", "\r\n", "\n\n", "\n- ", "\n1. ", "\n* ", "\n– ", "\n> ",
                  "\na. ", "\n[x] ", ". ", "; "]
        texts = []
        for n in range(300):
            parts = []
            for _ in range(rng.randint(1, 60 if n % 10 else 400)):
                roll = rng.random()
                if roll < 0.6:
                    token = rng.choice(atoms)
                    parts.append(token.capitalize() if rng.random() < 0.2 else token)
                elif roll < 0.85:
                    parts.append(rng.choice(specials))
                elif roll < 0.95:
                    parts.append("```\n" + " ".join(rng.choice(atoms) for _ in range(rng.randint(0, 6)))
                                 + "\n```")
                else:
                    parts.append("`" + rng.choice(atoms))
                parts.append(rng.choice(breaks))
            texts.append("".join(parts))
        self.assertTrue(any(len(t) > 2000 for t in texts))
        if not SLOW_TESTS:
            texts = texts[:40]  # same seed; the first 40 include notes over 2,000 characters
            self.assertTrue(any(len(t) > 2000 for t in texts))
        v2 = _frozen_v2()
        for files in (TRACKED, None):
            for text in texts:
                with self.subTest(text=text[:80], files=files):
                    for name, goal in (("goal", True), ("notes", False)):
                        frozen = v2._field_signals(name, text, v2._index(files), v2.RiskMap(), goal)
                        self.assertEqual(
                            {(s.id, s.detail) for s in
                             classifier.lane_v2_field_signals(name, text, files, None, goal)},
                            {(s.id, s.detail) for s in frozen})


def assert_drops_are_named(case, text, files, expected=None):
    """Every lane.v2 risky signal lane.v3 lacks is an 'exempted' signal; a lane falls only then.

    Returns the exemption names seen. With `expected`, any exemption must be exactly that name;
    without it, none may fire.
    """
    v2 = _frozen_v2()
    names = set()
    for field, args in (("goal", (text, [], "")), ("done[0]", (NEUTRAL_GOAL, [text], "")),
                        ("notes", (NEUTRAL_GOAL, [], text))):
        old = v2.task_lane(*args, files)
        new = task_lane(*args, files)
        exempted = {s.detail for s in new.signals if s.id == field + ":exempted"}
        new_ids = {(s.id, s.detail) for s in new.signals}
        for signal in old.signals:
            rule = signal.id.split(":", 1)[-1]
            if not signal.id.startswith(field + ":") or rule not in classifier._FIELD_RISKY:
                continue
            if (signal.id, signal.detail) in new_ids:
                continue
            suffix = " {}={}".format(rule, signal.detail)
            case.assertTrue([e for e in exempted if e.endswith(suffix)], (field, text, signal))
        for note in exempted:
            for name in note.split(" ", 1)[0].split("+"):
                case.assertIn(name, classifier.EXEMPTION_NAMES, (field, text, note))
                names.add(name)
        if field == "goal":
            case.assertFalse(exempted, text)
        if LANES.index(new.lane) < LANES.index(old.lane):
            case.assertTrue(exempted, (field, text, old.lane, new.lane))
    case.assertEqual(names, {expected} if expected else set(), text)
    return names


class V2SubsetTests(unittest.TestCase):
    """T68 r4: lane_v3 >= lane_v2 on the test corpus; a drop only through a named exemption."""

    def test_corpus(self):
        v2 = _frozen_v2()
        self.assertEqual(v2.CLASSIFIER_VERSION, "lane.v2")
        texts, v2_tests = corpus()
        self.assertGreater(len(texts), 250)
        expected = dict(ExemptionTests.EXEMPT)
        seen = set()
        for files in (v2_tests.TRACKED, TRACKED, None) if SLOW_TESTS else (TRACKED,):
            for text in texts:
                with self.subTest(text=text, files=files):
                    if assert_drops_are_named(self, text, files, expected.get(text)):
                        seen.add(text)
        self.assertEqual(seen, set(expected))


class TemplateGridTests(unittest.TestCase):
    """T68 r4: frozen lane.v2 against lane.v3 over the generated template grid."""

    def test_grid_never_drops_without_a_named_exemption(self):
        full = template_grid()
        self.assertGreaterEqual(len(full), 1000)
        grid = grid_cases()
        families = [full[0][0], full[273][0], full[309][0]]  # P1-1, P1-2, Turkish run
        for text in families:
            self.assertIn(text, {t for t, _ in grid})
        v2 = _frozen_v2()
        tagged, risky_v2 = set(), 0
        for text, name in grid:
            with self.subTest(text=text):
                if assert_drops_are_named(self, text, TRACKED, name):
                    tagged.add(text)
                risky_v2 += v2.task_lane(NEUTRAL_GOAL, [], text, TRACKED).lane == "risky"
        # Every expected exemption really fires (no stale tags).
        self.assertEqual(tagged, {t for t, name in grid if name})
        self.assertEqual({n for _, n in grid if n},
                         {"exempt:descriptive_subject", "exempt:tr_conditional_description"}
                         | {n for _, n in ExemptionTests.EXEMPT})
        self.assertGreater(risky_v2, 1000 if SLOW_TESTS else 150)
        # Fable's P1-1/P1-2 grids (273 + 36 sentences) stay risky.
        p1 = {t for t, _ in full[:273 + 36]}
        for text in [t for t, _ in grid if t in p1]:
            self.assertEqual(task_lane(NEUTRAL_GOAL, [], text, TRACKED).lane, "risky", text)


class ExemptionTests(unittest.TestCase):
    """T68 r4 (3)-(4): each named exemption, its sentence and its near misses."""

    EXEMPT = [
        ("Keep the pre and post conditions unchanged.", "exempt:prose_pre_post"),
        ("Branch and tag names stay the same.", "exempt:descriptive_subject"),
        ("The name and email fields are validated.", "exempt:descriptive_subject"),
        ("Tag names in the parser are case-insensitive.", "exempt:descriptive_subject"),
        ("Commit messages are validated.", "exempt:descriptive_subject"),
        ("Post titles stay under 80 characters.", "exempt:descriptive_subject"),
        ("Push notifications stay disabled.", "exempt:descriptive_subject"),
        ("Merge conflicts are reported.", "exempt:descriptive_subject"),
        ("The source of truth stays the committed fixture.", "exempt:prose_source_of_truth"),
        ("It starts with the tagline and the postal code.", "exempt:prose_starts_with"),
        ("The output is truncated when the suite starts.", "exempt:prose_starts_final"),
        ("No manager model call is made, and nothing is created under the configured state "
         "directory other than the published .orcexe outputs.", "exempt:prose_published_outputs"),
        ("makes no model call and publishes no plan.json", "exempt:prose_publishes_no"),
        ("The model call is mocked and nothing is published.", "exempt:prose_model_call"),
        ("Koşul sağlanırsa etiketler birleştirilir.", "exempt:tr_conditional_description"),
        ("Koşul sağlanırsa ayarlar yüklenir.", "exempt:tr_conditional_description"),
    ]
    NEAR_MISSES = [
        # descriptive_subject: a head outside the closed list, a modal, done/complete/happen.
        "Push branch is done.", "Deploy staging must happen before prod.",
        "Merge main must be done before release.", "Deploy scripts must be idempotent.",
        "Branch, tag and commit lists stay sorted.", "Push notifications are done.",
        "Tag names will stay the same.", "Push notifications stay on, then push the branch.",
        # prose_pre_post: an object after "post".
        "Keep the pre and post the results.",
        # Context: a real run verb next to the named noun use.
        "The source of truth stays the committed fixture and we run it.",
        "Start with the deploy script.", "Run the model call and push the branch.",
        "The model call starts the deploy.",
        # Outward words: another use of the same word.
        "Run the tests and the published docs stay unchanged.", "Run it so it publishes the plan.",
        # Turkish: no conditional, ile/üzerinden, a destination, an object outside the list.
        "Etiketler birleştirilir.", "Koşul sağlanırsa etiketler Slack ile paylaşılır.",
        "Koşul sağlanırsa etiketler Slack üzerinden paylaşılır.",
        "Koşul sağlanırsa etiketler sunucuya yüklenir.", "Koşul sağlanırsa rapor paylaşılır.",
        "Rapor Slack ile paylaşılır.", "PR birleştirilir.", "Paket yüklenir.",
        # T68 r5 (L2 P1-1): a context exemption drops only its own A5 words.
        "After the model call, publish docs.", "Source of truth: deploy main.",
        "A model call then pushes results.", "After each model call deployment begins.",
        "The rollout starts with a deploy to staging.", "The nightly job starts with deployment.",
        "The pipeline starts with an upload of the wheel.", "Once merged, the deploy starts.",
        "After approval the deployment starts.", "When tests pass the upload starts.",
        "Keep main as the source of truth for deployments.",
        # T68 r5 (L2 P2-1): publishes no / published outputs need their identifier context.
        "Start the job, which publishes no fewer than three packages.",
        "The job runs and publishes no draft, only the final release.",
        "Execute the job so the published outputs go live.",
        # T68 r5 (L2 P2-2): exact pairs, adjacency, Turkish conditional suffixes only.
        "CI geçerse etiketler paylaşılır.", "Onay varsa etiketler GitHub paylaşılır.",
        "İstenirse ayarlar S3 bucket yüklenir.", "Test false dönerse etiketler paylaşılır.",
        "Koşul sağlanırsa ayarlar birleştirilir.", "Release base case etiketler birleştirilir.",
    ]

    def test_each_exemption_drops_its_sentence_and_names_itself(self):
        names = set()
        for text, name in self.EXEMPT:
            with self.subTest(text=text):
                self.assertEqual(_frozen_v2().task_lane(NEUTRAL_GOAL, [], text, TRACKED).lane, "risky")
                self.assertNotEqual(task_lane(NEUTRAL_GOAL, [], text, TRACKED).lane, "risky")
                self.assertEqual(assert_drops_are_named(self, text, TRACKED, name), {name})
                names.add(name)
        # Every named exemption has at least one test sentence.
        self.assertEqual(names, set(classifier.EXEMPTION_NAMES))

    def test_near_misses_keep_the_v2_catch(self):
        for text in self.NEAR_MISSES:
            with self.subTest(text=text):
                self.assertEqual(_frozen_v2().task_lane(NEUTRAL_GOAL, [], text, TRACKED).lane, "risky")
                self.assertEqual(in_fields(text, NEUTRAL_GOAL), ("risky", "risky"))
                self.assertEqual(assert_drops_are_named(self, text, TRACKED), set())

    def test_exempted_signal_is_visible(self):
        result = task_lane(NEUTRAL_GOAL, ["Commit messages are validated."], "", TRACKED)
        self.assertIn(classifier.Signal("done[0]:exempted", "exempt:descriptive_subject A4=commit"),
                      result.signals)
        self.assertNotIn("exempted", classifier._FIELD_RISKY)

    def test_goal_is_never_exempted(self):
        for text, _ in self.EXEMPT:
            with self.subTest(text=text):
                result = task_lane(text, [], "", TRACKED)
                self.assertFalse([s for s in result.signals if s.id.endswith(":exempted")])
                self.assertEqual(result.lane, _frozen_v2().task_lane(text, [], "", TRACKED).lane)


class R4ReviewTests(unittest.TestCase):
    """T68 L2 (Fable) P1-1..P1-5 and the optional P2-1 additions."""

    RISKY = [
        # The eight sentences the main session verified.
        "Fix lint and push again.", "Fix the tests and deploy staging.",
        "Run the script to upload the results.", "Sonra deploy işini tetikle.",
        "Update version\nCommit\nPush\nDeploy", "Rapor Slack ile paylaşılır.", "Push branch is done.",
        "Update docs, tests, etc. Deploy the site.",
        # P1-1
        "Fix it and push main.", "Build and upload artifacts to S3.",
        # P1-2
        "Run the deployment.", "Run make to publish the docs.", "Run the tests before deploying.",
        "Son olarak push betiğini çalıştır.", "Source the deploy script.",
        # P1-3
        "Fix the docs\n– Push the branch", "Fix the docs\n— Deploy", "Fix the docs\n· Commit",
        "Fix the docs\n> Merge main", "Fix the docs\na. Publish the package",
        "Fix the docs\na) Upload the wheel", "Plan A. Push the branch.", "Compare old vs. new. Deploy.",
        # P1-4
        "PR birleştirilir.", "Paket yüklenir.", "Rapor Slack üzerinden paylaşılır.",
        # P1-5
        "Deploy staging must happen before prod.", "Merge main must be done before release.",
    ]
    OPTIONAL_P2 = [
        "Don't forget to push.", "Make sure to send it.", "Değişiklikleri push'la.", "Main'e merge'le.",
        "Sonucu ekibe mail at.", "Dosyayı sunucuya yüklemeyi unutma.", "Paketi yayımla.",
    ]

    def test_review_sentences_are_risky_in_done_and_notes(self):
        for text in self.RISKY + self.OPTIONAL_P2:
            with self.subTest(text=text):
                self.assertEqual(in_fields(text, NEUTRAL_GOAL), ("risky", "risky"))

    def test_optional_p2_twins_stay_quiet(self):
        for text in ("Make sure not to push.", "Remember not to send it.", "It is easy to push back on.",
                     "Ask how to tag the issue in the tracker.", "Make one commit at the end."):
            with self.subTest(text=text):
                self.assertNotIn("risky", in_fields(text, NEUTRAL_GOAL))


if __name__ == "__main__":
    unittest.main()
