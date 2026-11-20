"""Exec flag inventory and behavioral parity with the external fake parser.

The fake may implement explicitly forbidden manifest flags for negative safety
tests (currently --skip-git-repo-check). Every other accepted exec flag must be
producible by the manifest, and every producible flag must work in the fake.
"""

import ast
import sys

from tests.unit._timing import scaled
from tests.unit.test_gitutil import GitFixture, ROOT
from orcexe import proc
from orcexe.manifest import load_manifest


def parser_flags(path):
    """Inspect the fixture's parser, so an extra accepted flag also fails parity."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    parser = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and
                  node.name == "parse_exec")
    flags = set()
    for node in ast.walk(parser):
        if isinstance(node, ast.Dict):
            flags.update(key.value for key in node.keys if isinstance(key, ast.Constant) and
                         isinstance(key.value, str) and key.value.startswith("-") and key.value != "-")
        if (isinstance(node, ast.Compare) and isinstance(node.left, ast.Name) and
                node.left.id == "token" and len(node.ops) == 1 and isinstance(node.ops[0], ast.Eq)):
            flags.update(value.value for value in node.comparators if isinstance(value, ast.Constant) and
                         isinstance(value.value, str) and value.value.startswith("-") and value.value != "-")
    return flags


class ManifestParityTests(GitFixture):
    def test_manager_profile_contract(self):
        from orcexe.adapters import RunRequest
        brief = self.directory / 'brief.md'
        brief.write_text('Read only')
        for name in ('claude', 'codex', 'cursor'):
            manifest = load_manifest(ROOT / 'plugins/orcexe/adapters' / (name + '.yaml'))
            request = dict(run_id='manager', task='map', tool=manifest.tool, model='fixture',
                           effort='high', profile='manager', workdir=str(self.main),
                           brief_path=str(brief), run_dir=str(self.directory))
            if name != 'claude':
                self.assertNotIn('manager', manifest.data['exec']['profiles'])
                with self.assertRaises(ValueError):
                    RunRequest(**request)
                with self.assertRaises(ValueError):
                    manifest.build_argv('manager')
                continue
            RunRequest(**request)
            self.assertTrue(manifest.data['exec']['profiles']['manager']['read_only'])
            self.assertIn('--bare', manifest.data['exec']['forbidden_argv'])
            self.assertEqual(manifest.build_argv('manager', prompt='Read only', model='fixture',
                                                effort='high', run_dir=str(self.directory)),
                ['claude', '-p', 'Read only', '--output-format', 'stream-json', '--verbose',
                 '--model', 'fixture', '--effort', 'high', '--no-session-persistence',
                 '--strict-mcp-config', '--include-hook-events', '--add-dir',
                 str(self.directory / 'brief'), '--permission-mode', 'plan', '--restricted',
                 '--safe-mode', '--tools', 'Read,Glob,Grep'])

    def test_review_profile_argv_pins(self):
        profiles = {
            'claude': ['--permission-mode', 'plan', '--disallowedTools', 'Bash,WebFetch,WebSearch'],
            'codex': ['-s', 'read-only'],
            'cursor': ['--mode', 'plan'],
        }
        for name, extra in profiles.items():
            with self.subTest(tool=name):
                manifest = load_manifest(ROOT / 'plugins/orcexe/adapters' / (name + '.yaml'))
                self.assertEqual(list(manifest.data['exec']['profiles']['review']['extra']), extra)
                values = dict(workdir=str(self.main), model='fixture', effort='high',
                              final=str(self.directory / 'final.md'),
                              run_dir=str(self.directory), prompt='Read files only')
                argv = manifest.build_argv('review', **{
                    key: value for key, value in values.items() if key in manifest.placeholders('review')})
                self.assertEqual(argv[-len(extra):], extra)

    def test_every_manifest_profile_and_fake_flag_inventory(self):
        self.seed()
        fake_directory = ROOT / "tests" / "fixtures" / "fake-cli"
        manifests = sorted((ROOT / "plugins" / "orcexe" / "adapters").glob("*.y*ml"))
        covered = 0
        for path in manifests:
            manifest = load_manifest(path)
            fake = fake_directory / manifest.tool
            if not fake.is_file():
                continue
            covered += 1
            produced = set()
            for profile in manifest.data["exec"]["profiles"]:
                with self.subTest(manifest=path.name, profile=profile):
                    argv = manifest.build_argv(profile, workdir=str(self.main), model="gpt-6-luna",
                        effort="low", final=str(self.directory / "final.md"), prompt="Fixture parity probe")
                    produced.update(token for token in argv if token.startswith("-") and token != "-")
                    # Use the current interpreter directly: this is the fake,
                    # regardless of the user's PATH, installations or shims.
                    argv = [sys.executable, "-B", str(fake), *argv[1:]]
                    result = proc.run(argv, cwd=str(self.main), env=self.env,
                        timeout_s=scaled(3), stall_s=scaled(3), kill_grace_s=scaled(.1))
                    self.assertEqual(result.status, "exited")
                    self.assertNotEqual(result.returncode, 2, result.stderr_tail)
                    self.assertEqual(result.returncode, 0, result.stderr_tail)
            accepted = parser_flags(fake)
            forbidden = set(manifest.data["exec"]["forbidden_argv"])
            self.assertEqual(produced - accepted, set(), "manifest flag missing in fake")
            self.assertEqual(accepted - produced - forbidden, set(), "excess fake flag absent from manifest")
            unknown = "--orcexe-not-declared-in-manifest"
            self.assertNotIn(unknown, produced | forbidden)
            argv = [sys.executable, "-B", str(fake), "exec", "-C", str(self.main), unknown]
            result = proc.run(argv, cwd=str(self.main), env=self.env,
                timeout_s=scaled(3), stall_s=scaled(3), kill_grace_s=scaled(.1))
            self.assertEqual((result.status, result.returncode), ("exited", 2))
        self.assertGreater(covered, 0, "no manifest/fake pairs tested")
