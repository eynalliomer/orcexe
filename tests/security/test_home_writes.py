"""Full HOME snapshots permit only state and explicitly verified fake effects."""

import hashlib
import json
import os
import stat

from tests.security import SecurityFixture


def snapshot(home):
    result = {}
    for path in sorted(home.rglob("*")):
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            continue
        data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        result[path.relative_to(home).as_posix()] = (
            info.st_size, hashlib.sha256(data).hexdigest(), stat.S_IMODE(info.st_mode),
            stat.S_IFMT(info.st_mode))
    return result


def changes(before, after):
    return sorted(name for name in before.keys() | after.keys() if before.get(name) != after.get(name))


class HomeWriteTests(SecurityFixture):
    def test_all_flows_preserve_home_except_state_and_exact_codex_append(self):
        before = snapshot(self.home)
        self.run_flows()
        after = snapshot(self.home)
        modified = changes(before, after)
        allowed = {".codex/config.toml", ".cursor/cli-config.json"}
        # macOS system Python (Xcode/CLT) caches stdlib bytecode under the user's
        # Library when its own tree is read-only; that is the OS, not orcexe.
        os_caches = ("Library/Caches/com.apple.python/",)
        unexpected = [name for name in modified if name not in allowed and
                      not name.startswith(".cache/orcexe/") and
                      not name.startswith(os_caches)]
        self.assertEqual(unexpected, [], "unexpected HOME changes: " + repr(unexpected))
        self.assertTrue(any(name.startswith(".cache/orcexe/") for name in modified))
        # The Codex exception is exactly the fake's append, in execution order.
        expected = self.codex_original + b"".join(
            ('\n[projects.{}]\ntrust_level = "trusted"\n'.format(json.dumps(work, ensure_ascii=False))).encode("utf-8")
            for work in self.trusted_workdirs)
        self.assertEqual((self.home / ".codex/config.toml").read_bytes(), expected)
        self.assertEqual(before[".codex/config.toml"][2:], after[".codex/config.toml"][2:])
        # Cursor rewrites JSON formatting, but all five model keys and unrelated
        # sentinel data must end equal; its original permissions must survive.
        cursor = self.home / ".cursor/cli-config.json"
        self.assertEqual(json.loads(cursor.read_text()), self.cursor_original)
        self.assertEqual(before[".cursor/cli-config.json"][2:], after[".cursor/cli-config.json"][2:])

    def test_trust_clean_apply_changes_only_config_and_its_reported_backup(self):
        from orcexe import trust
        config = self.home / ".codex/config.toml"
        key = str(self.root / "missing-worktree")
        added = '\n[projects.{}]\ntrust_level = "trusted"\n'.format(json.dumps(key)).encode("utf-8")
        config.write_bytes(self.codex_original + added)
        trust.record(self.repo / ".orcexe/trust-ledger.json", frozenset(), frozenset({key}),
                     "security-clean", config)
        before = snapshot(self.home)
        report = json.loads(self.invoke("trust", "clean", "--repo", str(self.repo), "--apply", "--yes", "--json"))
        self.assertEqual(report["removed"], [key])
        self.assertTrue(report["confirmed"])
        from pathlib import Path
        backup = Path(report["backup"]).expanduser()
        self.assertEqual(backup.read_bytes(), self.codex_original + added)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
        # The section owns its header/body; the preceding separator stays put.
        self.assertEqual(config.read_bytes(), self.codex_original + b"\n")
        after = snapshot(self.home)
        self.assertEqual(changes(before, after), sorted((".codex/config.toml", backup.relative_to(self.home).as_posix())))
        self.assertEqual(before[".codex/config.toml"][2:], after[".codex/config.toml"][2:])
