"""Header-only observations and byte-preserving cleanup in private Codex homes."""

import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins/orcexe/src"))
from orcexe import trust


def header(key, ending=b"\n"):
    return ('[projects.' + json.dumps(str(key), ensure_ascii=False) + ']').encode() + ending


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=str(ROOT), prefix="orcexe-trust-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.codex = self.home / "codex"
        self.codex.mkdir()
        self.config = self.codex / "config.toml"
        self.ledger = self.root / ".orcexe/trust-ledger.json"
        self.env = mock.patch.dict(os.environ, HOME=str(self.home), CODEX_HOME=str(self.codex),
                                   CURSOR_CONFIG_DIR=str(self.home / "cursor"))
        self.env.start()
        self.addCleanup(self.env.stop)

    def record(self, *keys, before=frozenset()):
        return trust.record(self.ledger, before, frozenset(map(str, keys)), "run-1", self.config)

    def cursor_record(self, workspace):
        projects = trust.projects_path()
        key = trust.project_slug(workspace)
        (projects / key).mkdir(parents=True)
        return trust.record(self.ledger, frozenset(), frozenset({key}), "cursor-run", projects,
                            tool="cursor", workspaces=[workspace])[0]

    @unittest.skipIf(os.name == "nt", "Measured POSIX absolute paths")
    def test_cursor_slug_matches_two_real_samples(self):
        samples = {
            "/Users/u/repos/orcexe-wt/do/do-20261117t200438-809b-cursor":
                "Users-u-repos-orcexe-wt-do-do-20261117t200438-809b-cursor",
            "/Users/u/repos/orcexe/.orcexe/runs/do-20261117T200438-809b-r1":
                "Users-u-repos-orcexe-orcexe-runs-do-20261117T200438-809b-r1",
        }
        for workspace, key in samples.items():
            with self.subTest(workspace=workspace):
                self.assertEqual(trust.project_slug(workspace), key)

    def test_cursor_slug_collision_is_unknown_and_never_cleaned(self):
        workspaces = [self.root / "a.b", self.root / "a-b"]
        key = trust.project_slug(workspaces[0])
        self.assertEqual(trust.project_slug(workspaces[1]), key)
        projects = trust.projects_path()
        (projects / key).mkdir(parents=True)
        entry, = trust.record(self.ledger, frozenset(), frozenset({key}), "collision", projects,
                              tool="cursor", workspaces=workspaces)
        self.assertIsNone(entry["workspace"])
        before = self.ledger.read_bytes()
        confirm = mock.Mock(return_value=True)
        report = trust.clean(self.ledger, apply=True, confirm=confirm)
        self.assertEqual(report["skipped_unknown"], [key])
        self.assertEqual(report["candidates"], [])
        self.assertEqual(report["removed"], [])
        confirm.assert_not_called()
        self.assertEqual(self.ledger.read_bytes(), before)
        self.assertTrue((projects / key).is_dir())

    def test_cursor_observation_and_cleanup_never_open_private_contents(self):
        projects = trust.projects_path()
        projects.mkdir(parents=True)
        (projects / "pre-existing").mkdir()
        before = trust.project_names()
        gone = self.root / "gone"
        entry = self.cursor_record(gone)
        self.assertEqual(trust.record(self.ledger, before, trust.project_names(), "again", projects,
                                     tool="cursor", workspaces=[gone]), [])
        key = entry["key"]
        # Prevent even an attempted content read under projects, including archive.
        original_open = Path.open
        def private_open(path, *args, **kwargs):
            if projects in path.parents:
                raise AssertionError("Cursor private contents opened")
            return original_open(path, *args, **kwargs)
        with mock.patch.object(Path, "open", private_open):
            self.assertEqual([e["key"] for e in trust.list_entries(self.ledger)], [key])
            callback = mock.Mock(return_value=True)
            self.assertEqual(trust.clean(self.ledger, confirm=callback)["candidates"], [key])
            callback.assert_not_called()
            before_bytes = self.ledger.read_bytes()
            self.assertFalse(trust.clean(self.ledger, apply=True)["confirmed"])
            self.assertEqual(self.ledger.read_bytes(), before_bytes)
            report = trust.clean(self.ledger, apply=True, confirm=callback)
            callback.assert_called_once_with([key])
            self.assertEqual(report["removed"], [key])
            removed = trust.list_entries(self.ledger)[0]
            self.assertEqual(removed["state"], "removed")
            self.assertTrue(Path(removed["backup"]).expanduser().is_dir())
            self.assertFalse((projects / key).exists())
            self.assertTrue((projects / "pre-existing").is_dir())
            self.assertEqual(trust.clean(self.ledger, apply=True)["removed"], [])

    def test_cursor_unknown_missing_live_and_other_config_are_kept(self):
        live = self.root / "live"
        live.mkdir()
        self.cursor_record(live)
        missing = self.cursor_record(self.root / "missing")
        (trust.projects_path() / missing["key"]).rmdir()
        projects = trust.projects_path()
        (projects / "unknown-slug").mkdir()
        trust.record(self.ledger, frozenset(), frozenset({"unknown-slug"}), "unknown", projects, tool="cursor")
        self.assertIsNone(trust.list_entries(self.ledger)[-1]["workspace"])
        report = trust.clean(self.ledger)
        self.assertEqual(report["kept_live"], [trust.project_slug(live)])
        self.assertEqual(report["skipped_unknown"], ["unknown-slug"])
        self.assertEqual(report["candidates"], [])
        self.assertEqual(trust.list_entries(self.ledger)[1]["state"], "missing")
        other = self.home / "other-cursor"
        (other / "projects" / trust.project_slug(live)).mkdir(parents=True)
        self.assertEqual(trust.clean(self.ledger, cursor_config_dir=other)["candidates"], [])

    def test_cursor_refuses_symlinks_and_workspace_reappearing_at_confirmation(self):
        gone = self.root / "gone"
        entry = self.cursor_record(gone)
        projects = trust.projects_path()
        source = projects / entry["key"]
        before = self.ledger.read_bytes()
        def appeared(keys):
            gone.mkdir()
            return True
        with self.assertRaisesRegex(trust.TrustError, "appeared during clean"):
            trust.clean(self.ledger, apply=True, confirm=appeared)
        self.assertTrue(source.is_dir())
        self.assertEqual(self.ledger.read_bytes(), before)
        gone.rmdir()
        if not hasattr(os, "symlink"):
            return
        target = self.root / "user-dir"
        target.mkdir()
        source.rmdir()
        source.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(trust.TrustError, "symlink"):
            trust.clean(self.ledger, apply=True, confirm=lambda keys: True)
        source.unlink()
        source.mkdir()
        archive = projects / ".orcexe-removed"
        archive.symlink_to(target, target_is_directory=True)
        with self.assertRaisesRegex(trust.TrustError, "symlink"):
            trust.clean(self.ledger, apply=True, confirm=lambda keys: True)
        self.assertTrue(source.is_dir())
        self.assertEqual(self.ledger.read_bytes(), before)

    def test_cursor_validator_rejects_unsafe_names_and_workspace_mapping(self):
        if os.name != "nt":
            self.assertEqual(trust.project_slug("/-workspace/path-"), "workspace-path")
        self.cursor_record(self.root / "gone")
        original = json.loads(self.ledger.read_text())
        for field, value in (("key", "../escape"), ("key", ".orcexe-removed"),
                             ("workspace", "relative"), ("workspace", str(self.root / "wrong"))):
            data = json.loads(json.dumps(original))
            data["entries"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaises(trust.TrustError):
                trust.validate(data)

    def test_headers_escapes_unicode_spaces_and_exact_anchor(self):
        keys = {'C:\\work\\quote"here', 'a space/日本語', 'plain'}
        text = b'\r\n'.join(header(k, b'') for k in keys)
        text += (b'\r\n# [projects."comment"]\r\n [projects."indented"]\r\n'
                 b'[projects."x".sub]\r\nsecret = \'[projects."in-string"]\'\r\n'
                 b'[projects."prefix"] # comment\r\n[projects."a\\u0020b\\U0001f600"]\r\n')
        self.config.write_bytes(text)
        self.assertEqual(trust.headers(), frozenset(keys | {'a b😀'}))
        self.assertEqual(trust.snapshot(), trust.headers(self.codex))
        self.config.unlink()
        self.assertEqual(trust.headers(), frozenset())

    def test_scan_retains_only_headers_and_never_bulk_reads(self):
        secret = "private-" + os.urandom(24).hex()
        self.config.write_text('secret = "' + secret + '"\n' + header("project").decode())
        original = Path.open
        observations = []

        class LinesOnly:
            def __init__(self, handle):
                self.handle = handle
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.handle.close()
            def readline(self, limit):
                observations.append(limit)
                return self.handle.readline(limit)
            def read(self, *args):
                raise AssertionError("bulk read of config")

        def opened(path, *args, **kwargs):
            handle = original(path, *args, **kwargs)
            return LinesOnly(handle) if path == self.config else handle

        with mock.patch.object(Path, "open", opened):
            before = trust.headers()
            after = trust.snapshot()
            added = trust.record(self.ledger, frozenset(), after, "run-1", self.config)
            listed = trust.list_entries(self.ledger)
        preview = trust.clean(self.ledger)
        for returned in (before, after, added, listed, preview):
            self.assertNotIn(secret, repr(returned))
        self.assertNotIn(secret.encode(), self.ledger.read_bytes())
        self.assertTrue(observations)
        self.assertTrue(all(0 < n <= trust.LIMIT + 1 for n in observations))

    def test_size_limit_including_one_huge_line(self):
        self.config.write_bytes(b'#' * trust.LIMIT)
        self.assertEqual(trust.headers(), frozenset())
        with self.config.open('ab') as handle:
            handle.write(b'!')
        for operation in (trust.headers, lambda: trust.clean(self.ledger)):
            with self.assertRaisesRegex(trust.TrustError, "8 MiB"):
                operation()
        self.config.write_bytes((b'#\n' * (trust.LIMIT // 2)) + b'!')
        with self.assertRaises(trust.TrustError):
            trust.headers()

    def test_path_resolution_uses_current_environment(self):
        self.assertEqual(trust.config_path(), self.config)
        other = self.root / "other"
        other.mkdir()
        (other / "config.toml").write_bytes(header("other"))
        with mock.patch.dict(os.environ, CODEX_HOME=str(other)):
            self.assertEqual(trust.headers(), frozenset({"other"}))
        with mock.patch.dict(os.environ, CODEX_HOME=""):
            self.assertEqual(trust.config_path(), self.home / ".codex/config.toml")
            with mock.patch.dict(os.environ, HOME=str(other)):
                self.assertEqual(trust.config_path(), other / ".codex/config.toml")

    def test_record_preexisting_dedup_ids_and_permissions(self):
        self.assertEqual(self.record("user", before=frozenset({"user"})), [])
        self.assertFalse(self.ledger.exists())
        added = self.record("user", "new", before=frozenset({"user"}))
        self.assertEqual([e["key"] for e in added], ["new"])
        self.assertEqual(added[0]["id"], "TL-0001")
        self.assertEqual(added[0]["file"], "~/codex/config.toml")
        self.assertEqual(self.record("new"), [])
        self.assertEqual(self.record("next")[0]["id"], "TL-0002")
        self.assertEqual(stat.S_IMODE(self.ledger.stat().st_mode), 0o600)
        self.assertEqual(trust.validate(json.loads(self.ledger.read_text()))["schema"], trust.SCHEMA)

    def test_validator_rejects_unknown_fields_states_types_and_ids(self):
        self.record("new")
        original = json.loads(self.ledger.read_text())
        mutations = [("unexpected", True), ("state", "owned"), ("id", "TL-0000"),
                     ("removed_at", "yesterday"), ("key", 1), ("observed_at", "not-a-date"),
                     ("backup", "not-removed"), ("origin", "user")]
        for field, value in mutations:
            data = json.loads(json.dumps(original))
            data["entries"][0][field] = value
            with self.subTest(field=field), self.assertRaises(trust.TrustError):
                trust.validate(data)
        for data in ({**original, "extra": 1}, {**original, "schema": "unknown"},
                     {**original, "entries": original["entries"] * 2}):
            with self.assertRaises(trust.TrustError):
                trust.validate(data)

    def test_list_refresh_is_read_only_and_removed_stays_removed(self):
        self.config.write_bytes(header("new"))
        self.record("new", "missing")
        before = self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns
        self.assertEqual({e["key"]: e["state"] for e in trust.list_entries(self.ledger)},
                         {"new": "present", "missing": "missing"})
        self.assertEqual(before, (self.ledger.read_bytes(), self.ledger.stat().st_mtime_ns))
        trust.clean(self.ledger, apply=True, confirm=lambda keys: True)
        self.config.write_bytes(header("new"))
        self.assertEqual({e["key"]: e["state"] for e in trust.list_entries(self.ledger)}["new"], "removed")
        # The listing before cleaning made no persistent refresh.
        self.assertEqual(json.loads(before[0])["entries"][0]["state"], "present")

    def test_clean_preserves_bytes_mode_live_user_edited_and_second_noop(self):
        for ending, trailing in ((b'\r\n', True), (b'\n', False)):
            with self.subTest(ending=ending, trailing=trailing):
                if self.ledger.exists():
                    self.ledger.unlink()
                gone = self.root / ('gone-' + str(trailing))
                live = self.root / ('live-' + str(trailing))
                live.mkdir()
                edited = self.root / ('edited-' + str(trailing))
                user = self.root / 'user'
                prefix = b'secret = "unparsed"' + ending + b'# keep this' + ending
                remove = header(gone, ending) + b'trust_level = "trusted" # OK' + ending + ending
                rest = (header(live, ending) + b'trust_level = "trusted"' + ending
                        + header(edited, ending) + b'trust_level = "trusted"' + ending
                        + b'custom = true' + ending + header(user, ending)
                        + b'trust_level = "trusted"' + ending
                        + b'  [unrelated]' + ending + b'value = "keep"' + (ending if trailing else b''))
                original = prefix + remove + rest
                self.config.write_bytes(original)
                os.chmod(self.config, 0o640)
                self.record(gone, live, edited)
                stamp = self.config.stat().st_mtime_ns, self.ledger.stat().st_mtime_ns
                ledger_bytes = self.ledger.read_bytes()
                callback = mock.Mock(return_value=True)
                report = trust.clean(self.ledger, confirm=callback)
                callback.assert_not_called()
                self.assertEqual(report['candidates'], [str(gone)])
                self.assertEqual(report['kept_live'], [str(live)])
                self.assertEqual(report['skipped_edited'], [str(edited)])
                self.assertEqual(self.config.read_bytes(), original)
                self.assertEqual(self.ledger.read_bytes(), ledger_bytes)
                self.assertEqual(stamp, (self.config.stat().st_mtime_ns, self.ledger.stat().st_mtime_ns))
                report = trust.clean(self.ledger, apply=True, confirm=callback)
                callback.assert_called_once_with([str(gone)])
                self.assertEqual(self.config.read_bytes(), prefix + rest)
                backup = Path(report['backup']).expanduser()
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
                removed = next(e for e in trust.list_entries(self.ledger) if e['key'] == str(gone))
                self.assertEqual(removed['state'], 'removed')
                self.assertEqual(removed['backup'], report['backup'])
                self.assertTrue(removed['removed_at'])
                callback.reset_mock()
                current = self.config.read_bytes(), self.config.stat().st_mtime_ns, self.ledger.read_bytes()
                self.assertEqual(trust.clean(self.ledger, apply=True, confirm=callback)['removed'], [])
                callback.assert_not_called()
                self.assertEqual(current, (self.config.read_bytes(), self.config.stat().st_mtime_ns, self.ledger.read_bytes()))

    def test_eof_table_without_trailing_newline_and_backup_collision(self):
        gone = self.root / 'gone'
        self.config.write_bytes(b'# prefix\r\n' + header(gone, b'\r\n') + b'trust_level = "trusted"')
        original = self.config.read_bytes()
        base_backup = self.config.with_name('config.toml.orcexe.bak')
        base_backup.write_bytes(b'old backup')
        self.record(gone)
        report = trust.clean(self.ledger, apply=True, confirm=lambda keys: True)
        self.assertEqual(self.config.read_bytes(), b'# prefix\r\n')
        self.assertEqual(base_backup.read_bytes(), b'old backup')
        self.assertTrue(report['backup'].startswith('~/codex/config.toml.orcexe.bak.'))
        self.assertEqual(Path(report['backup']).expanduser().read_bytes(), original)

    def test_refusal_and_concurrent_change_do_not_write(self):
        self.config.write_bytes(header('gone') + b'trust_level = "trusted"\n')
        self.record('gone')
        original = self.config.read_bytes(), self.ledger.read_bytes()
        self.assertFalse(trust.clean(self.ledger, apply=True)['confirmed'])
        self.assertEqual(original, (self.config.read_bytes(), self.ledger.read_bytes()))

        def changed(keys):
            self.config.write_bytes(b'# changed\n' + original[0])
            return True
        with self.assertRaisesRegex(trust.TrustError, 'changed during clean'):
            trust.clean(self.ledger, apply=True, confirm=changed)
        self.assertEqual(self.config.read_bytes(), b'# changed\n' + original[0])
        self.assertEqual(self.ledger.read_bytes(), original[1])
        self.assertEqual(list(self.codex.glob('*.bak*')), [])

    def test_change_during_copy_detected_and_not_replaced(self):
        self.config.write_bytes(header('gone') + b'trust_level = "trusted"\n')
        self.record('gone')
        original = self.config.read_bytes()
        digest = trust._digest
        calls = []
        def changing(path):
            calls.append(path)
            if len(calls) == 2:
                path.write_bytes(original + b'# concurrent\n')
            return digest(path)
        with mock.patch.object(trust, '_digest', side_effect=changing):
            with self.assertRaisesRegex(trust.TrustError, 'changed during clean'):
                trust.clean(self.ledger, apply=True, confirm=lambda keys: True)
        self.assertEqual(self.config.read_bytes(), original + b'# concurrent\n')
        self.assertEqual(json.loads(self.ledger.read_text())['entries'][0]['state'], 'present')
        self.assertFalse(list(self.codex.glob('.config.toml-orcexe-*')))

    def test_other_config_cannot_authorize_removal(self):
        self.record('gone')
        other = self.root / 'other'
        other.mkdir()
        config = other / 'config.toml'
        config.write_bytes(header('gone') + b'trust_level = "trusted"\n')
        before = config.read_bytes()
        report = trust.clean(self.ledger, other, apply=True, confirm=lambda keys: True)
        self.assertEqual(report['removed'], [])
        self.assertEqual(config.read_bytes(), before)

    def test_user_edited_trust_value_and_subtable_boundaries(self):
        keys = ['extra', 'untrusted', 'safe']
        self.config.write_bytes(header('extra') + b'trust_level = "trusted"\nvalue=1\n'
                                + header('untrusted') + b'trust_level = "untrusted"\n'
                                + header('safe') + b'# comment\ntrust_level = "trusted"\n'
                                + b'[projects."safe".sub]\nvalue=2')
        self.record(*keys)
        original = self.config.read_bytes()
        report = trust.clean(self.ledger, apply=True, confirm=lambda keys: True)
        self.assertEqual(report['skipped_edited'], ['extra', 'untrusted'])
        self.assertEqual(report['removed'], ['safe'])
        self.assertEqual(self.config.read_bytes(), original.replace(
            header('safe') + b'# comment\ntrust_level = "trusted"\n', b''))


if __name__ == '__main__':
    unittest.main()
