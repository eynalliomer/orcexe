"""U1 uses committed, bounded evidence, with no adapter or project execution."""

from contextlib import redirect_stdout
import builtins
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest import TestCase, mock

from tests.unit.test_gitutil import GitFixture
from tests.security import capture_children
from tests.security.test_no_pii_in_outputs import pii_canaries, PRIVATE_KEYS
from orcexe import cli, intake, proc


class CleanTests(TestCase):
    def test_code_at_predicate_is_importable_and_checks_context(self):
        from orcexe.intake import _code_at

        marker = chr(64) + 'unittest.' + 'skip'
        for prefix in ('', ' ', '\t', '\n', '`', "'", '"', '(', '[', '{', '<',
                       ' (`\'', '\u2003('):
            text = prefix + marker
            with self.subTest(prefix=prefix):
                self.assertTrue(_code_at(text, len(prefix)))
        for prefix in ('x', 'é', '1', '_', '+', '-', '.', '/', ':',
                       "fixture'", 'fixture`', "fixture''", 'fixture``',
                       'label:(', 'https:' + '//('):
            text = prefix + marker
            with self.subTest(prefix=prefix):
                self.assertFalse(_code_at(text, len(prefix)))
        for tail in ('plain', '1module.name', 'module.1name', 'module.',
                     'module.name/' + 'https:' + '//host'):
            with self.subTest(tail=tail):
                self.assertFalse(_code_at(chr(64) + tail, 0))
        text = 'label: https:' + '//host ' + marker + ':reason'
        self.assertTrue(_code_at(text, text.index(chr(64))))
        self.assertFalse(_code_at('module.name', 0))

    def test_clean_preserves_code_markers_bare_and_wrapped(self):
        for module in ('unittest.', 'pytest.mark.'):
            for suffix in ('', "('fixture reason')"):
                marker = chr(64) + module + 'skip' + suffix
                for left, right in (('', ''), ('`', '`'), ("'", "'"),
                                    ('"', '"'), ('(', ')'), ('[', ']'),
                                    ('{', '}'), ('<', '>')):
                    text = left + marker + right
                    with self.subTest(module=module, suffix=suffix, left=left):
                        self.assertEqual(intake._clean(text), text)

    def test_clean_still_redacts_addresses_with_punctuation(self):
        for ending in ('', '+', '-', "'", '`', "''", '``'):
            address = 'fixture' + ending + chr(64) + 'example.invalid'
            for left, right in (('', ''), ('`', '`'), ("'", "'"), ('"', '"')):
                with self.subTest(ending=ending, left=left):
                    result = intake._clean(left + address + right)
                    self.assertEqual(result.strip('`\'"'), '[REDACTED:pii]')

    def test_clean_still_redacts_url_userinfo_with_punctuation(self):
        for ending in ('!', '$', ')', ']', '.', '-'):
            userinfo = 'ad' + 'min:' + 'Fixture9Value' + ending
            text = 'https:' + '//' + userinfo + chr(64) + 'example.invalid/path'
            with self.subTest(ending=ending):
                result = intake._clean(text)
                self.assertNotIn(userinfo, result)
                self.assertNotIn('Fixture9Value', result)
                self.assertIn('[REDACTED:', result)

    def test_clean_protects_only_code_at_signs_before_redaction(self):
        marker = chr(64) + 'unittest.' + 'skip'
        address = 'fixture' + chr(64) + 'example.invalid'
        assignment = 'pass' + 'word' + '=' + 'Fixture9Value'
        text = marker + ' ' + address + ' ' + assignment
        with mock.patch.object(intake, 'redact_text', wraps=intake.redact_text) as redact:
            result = intake._clean(text)
        redact.assert_called_once_with('\ue000' + marker[1:] + ' ' + address + ' ' + assignment)
        self.assertIn(marker, result)
        self.assertIn('[REDACTED:pii]', result)
        self.assertNotIn(address, result)
        self.assertNotIn('Fixture9Value', result)
        self.assertNotIn('\ue000', result)

    def test_clean_existing_sentinel_skips_protection_and_restoration(self):
        marker = chr(64) + 'pytest.mark.' + 'skip'
        text = '\ue000 ' + marker
        with mock.patch.object(intake, 'redact_text', return_value=text) as redact:
            self.assertEqual(intake._clean(text), text)
        redact.assert_called_once_with(text)
        self.assertEqual(intake._clean('\ue000'), '\ue000')

    def test_clean_many_at_signs_in_one_token_finishes_within_budget(self):
        script = '''
from unittest import mock
from orcexe import intake

for prefix in ('(', '<', '((', ' '):
    unit = prefix + chr(64) + 'a.b'
    text = unit * (intake.FILE_CAP // len(unit))
    with mock.patch.object(intake, 'redact_text', side_effect=lambda value: value):
        if intake._clean(text) != text:
            raise AssertionError('code marker round trip failed')
'''
        env = dict(os.environ, PYTHONPATH=str(Path(intake.__file__).resolve().parents[1]),
                   PYTHONDONTWRITEBYTECODE='1')
        result = subprocess.run([sys.executable, '-c', script], env=env,
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)


class IntakeTests(GitFixture):
    def write(self, path, content):
        target = self.main / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content if isinstance(content, bytes) else content.encode())

    def commit(self, files):
        self.write('.gitignore', '.orcexe/\n')
        for path, content in files.items():
            self.write(path, content)
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '--quiet', '-m', 'synthetic U1 fixture')

    def scan(self, repo=None):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return intake.scan(repo or self.main)[1]

    def python_fixture(self):
        self.commit({
            'pyproject.toml': '[project]\nname = "sample-python"\nversion = "1.2.3"\n'
                              '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
                              '[tool.ruff]\nline-length = 88\n[tool.mypy]\nstrict = true\n',
            'requirements-dev.txt': 'pytest\n',
            'uv.lock': 'not parsed',
            '.github/workflows/test.yml': 'name: tests\non: [push]\njobs:\n  test:\n'
                    '    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n'
                    '        os: [ubuntu-latest, windows-latest]\n'
                    '        python-version: ["3.9", "3.13"]\n'
                    '    steps:\n      - run: python3 -m pytest\n'
                    '      - run: |\n          python3 -m ruff check .\n          python3 -m mypy .\n'
                    '  lint:\n    runs-on: ubuntu-latest\n    steps:\n      - run: echo ready\n',
            'Makefile': 'test lint: dependency\n\tfalse\nformat:\n\tfalse\n',
            'justfile': 'build release="no":\n  false\n',
            'tox.ini': '[tox]\nenvlist = py39, py313\n[testenv:lint]\ncommands = ruff check .\n',
            'docs/adr/001.md': 'private ADR contents\n',
            'architecture/decisions/a.md': 'private decision contents\n',
            'ROADMAP.md': 'private roadmap contents\n',
            'TODO.txt': 'private todo contents\n',
            'CHANGELOG.md': 'private changelog contents\n',
            'src/main.py': 'raise RuntimeError("never execute project")\n',
        })

    def typescript_fixture(self):
        self.commit({
            'package.json': json.dumps({'name': 'sample-ts', 'version': '2.0.0', 'workspaces': ['packages/*'],
                             'scripts': {'test': 'vitest run', 'lint': 'eslint .', 'build': 'tsc',
                                         'format': 'prettier --write .', 'start': 'node app.js'}}),
            'packages/a/package.json': '{"name":"child-a","scripts":{"test":"vitest"}}',
            'packages/b/package.json': '{"name":"child-b"}',
            'pnpm-lock.yaml': 'invalid! this lock is only presence evidence',
            '.claude/settings.json': json.dumps({'hooks': {'PreToolUse': [{'hooks': [{'type': 'command',
                                               'command': 'DO_NOT_RETURN_HOOK_COMMAND'}]}]},
                                               'private': 'DO_NOT_RETURN_TOOL_SETTINGS'}),
            '.codex/config.toml': 'notify = ["DO_NOT_RETURN_NOTIFY_COMMAND"]\n',
            '.cursor/hooks.json': '{"hooks":{"beforeShellExecution":[{"command":"DO_NOT_RETURN_CURSOR_HOOK"}]}}',
            '.mcp.json': json.dumps({'mcpServers': {'local': {'command': '/opt/tools/node',
                         'args': ['DO_NOT_RETURN_MCP_ARGUMENT'], 'env': {'TOKEN': 'DO_NOT_RETURN_ENV'}}}}),
            '.cursor/mcp.json': '{"mcpServers":{"windows":{"command":"C:\\\\tools\\\\server.exe"}}}',
            '.vscode/mcp.json': '{"servers":{"remote":{"url":"https://example.invalid/private"}}}',
            '.husky/pre-commit': 'DO_NOT_READ_HOOK\n',
            '.githooks/pre-push': 'DO_NOT_READ_GIT_HOOK\n',
            '.pre-commit-config.yaml': 'DO_NOT_READ_PRE_COMMIT\n',
        })

    def test_python_stack_commands_ci_docs_size_and_meta(self):
        self.python_fixture()
        report = self.scan()
        self.assertEqual(report['schema'], 'orcexe.intake.u1.v1')
        package = next(v for v in report['stack'] if v['path'] == 'pyproject.toml')
        self.assertEqual(package, dict(path='pyproject.toml', ecosystem='python', name='sample-python',
                                       version='1.2.3', lock_files=['uv.lock']))
        self.assertEqual({v['path'] for v in report['stack']}, {'pyproject.toml', 'requirements-dev.txt'})
        commands = report['commands']
        for argv in (['python3', '-m', 'pytest'], ['python3', '-m', 'ruff', 'check', '.'],
                     ['python3', '-m', 'mypy'], ['make', 'test'], ['make', 'lint'], ['make', 'format'],
                     ['just', 'build'], ['tox', '-e', 'py39'], ['tox', '-e', 'py313'], ['tox', '-e', 'lint']):
            self.assertIn(argv, [v.get('argv') for v in commands])
        self.assertEqual({v['kind'] for v in commands}, {'test', 'lint', 'build', 'format', 'other'})
        for command in commands:
            self.assertIs(command['verified'], False)
            self.assertEqual(set(command), {'id', 'kind', 'source', 'verified',
                                            'argv' if 'argv' in command else 'script'})
            self.assertRegex(command['source'], r'^file:.+:\d+$')
            source, number = command['source'][5:].rsplit(':', 1)
            self.assertLessEqual(int(number), len((self.main / source).read_text().splitlines()))
        ci = report['ci']
        self.assertEqual(ci['provider'], 'GitHub Actions')
        self.assertEqual(ci['workflow_files'], ['.github/workflows/test.yml'])
        self.assertEqual(ci['runs-on'], ['ubuntu-latest'])
        self.assertEqual(ci['matrix'], {'os': ['ubuntu-latest', 'windows-latest'], 'python-version': ['3.13', '3.9']})
        self.assertIn('python3 -m ruff check .\npython3 -m mypy .\n', [v.get('script') for v in commands])
        self.assertEqual(len(report['docs']), 5)
        self.assertTrue(all(set(v) == {'path', 'size'} for v in report['docs']))
        tracked = self.git_run(self.main, 'ls-files', '-z').split('\0')[:-1]
        self.assertEqual(report['size']['tracked_file_count'], len(tracked))
        self.assertEqual(report['size']['bytes'], sum((self.main / p).stat().st_size for p in tracked))
        self.assertEqual(report['size']['languages']['.py'], {'files': 1, 'bytes': (self.main / 'src/main.py').stat().st_size})
        meta = report['meta']
        self.assertEqual(meta['head'], self.git_run(self.main, 'rev-parse', 'HEAD').strip())
        self.assertFalse(meta['dirty'])
        self.assertEqual(meta['untracked_count'], 0)
        self.assertRegex(meta['generated_at'], r'Z$')
        self.assertRegex(meta['orcexe_version'], r'^\d+\.\d+\.\d+')
        self.assertFalse(meta['unseen'])

    def test_workspace_security_is_allow_listed_and_never_invoked(self):
        self.typescript_fixture()
        with mock.patch.dict(os.environ, dict(self.env, ORCEXE_BIN_CODEX='/never/codex',
                             ORCEXE_BIN_CLAUDE='/never/claude', ORCEXE_BIN_CURSOR='/never/agent'), clear=True):
            with capture_children() as children:
                report = intake.scan(self.main)[1]
        self.assertTrue(children)
        self.assertTrue(all(Path(child['argv'][0]).name == 'git' for child in children))
        self.assertTrue(all(any(frame[0] == 'orcexe/proc.py' for frame in child['frames']) for child in children))
        self.assertEqual(report['size']['package_roots'], ['.', 'packages/a', 'packages/b'])
        root = next(v for v in report['stack'] if v['path'] == 'package.json')
        self.assertEqual(root['lock_files'], ['pnpm-lock.yaml'])
        self.assertEqual((root['name'], root['version']), ('sample-ts', '2.0.0'))
        self.assertEqual(report['security']['risk'], 'present')
        self.assertIn({'path': '.mcp.json', 'name': 'local', 'command': 'node'}, report['security']['mcp'])
        self.assertIn({'path': '.cursor/mcp.json', 'name': 'windows', 'command': 'server.exe'}, report['security']['mcp'])
        hooks = {v['path'] for v in report['security']['hooks']}
        self.assertTrue({'.claude/settings.json', '.codex/config.toml', '.cursor/hooks.json', '.husky/pre-commit',
                         '.githooks/pre-push', '.pre-commit-config.yaml', 'git-config:core.hooksPath'} <= hooks)
        self.assertNotIn('DO_NOT_', json.dumps(report))
        self.assertNotIn('example.invalid/private', json.dumps(report))

    def test_no_commits_is_empty_with_unseen(self):
        report = self.scan()
        self.assertEqual(report['stack'], [])
        self.assertEqual(report['commands'], [])
        self.assertEqual(report['ci']['provider'], 'none')
        self.assertIsNone(report['meta']['head'])
        self.assertIn({'path': 'HEAD', 'reason': 'no_head'}, report['meta']['unseen'])
        self.assertEqual(report['size']['tracked_file_count'], 0)

    def test_secret_choke_point_and_fetch_allowlist(self):
        secrets = ('.env', '.env.local', '.ENV.production', 'nested/key.pem', 'private.key', 'id_rsa.pub',
                   'auth.p8', 'auth.p12', 'credentials.json', 'secrets.toml', '.npmrc', '.pypirc', '.netrc',
                   '.claude/secrets.json', '.cursor/rules/credentials.md', 'secrets/package.json')
        files = {path: 'NEVER_FETCH_' + path for path in secrets}
        files.update({'package.json': '{"name":"public"}', 'app.py': 'NEVER_FETCH_SOURCE',
                      '.husky/pre-commit': 'NEVER_FETCH_HOOK', 'README.md': 'NEVER_FETCH_DOC',
                      '.mcp.json': '{"mcpServers":{}}'})
        self.commit(files)
        oids = {path: self.git_run(self.main, 'rev-parse', 'HEAD:' + path).strip() for path in files}
        original = intake._git
        requests = []

        def observe(repo, *args, **kwargs):
            if args[:2] == ('cat-file', '--batch'):
                requests.extend(kwargs['input_data'].decode().splitlines())
            return original(repo, *args, **kwargs)
        with mock.patch.object(intake, '_git', side_effect=observe):
            report = self.scan()
        self.assertEqual(set(requests), {oids['package.json'], oids['.mcp.json']})
        for path in secrets:
            self.assertNotIn(oids[path], requests)
            self.assertIn({'path': path, 'reason': 'secret_path'}, report['meta']['unseen'])
        self.assertNotIn('NEVER_FETCH_', json.dumps(report))

    def test_dirty_and_untracked_are_counted_never_discovered(self):
        self.commit({'package.json': '{"name":"committed","scripts":{"test":"safe command"}}'})
        before = self.scan()
        self.write('package.json', '{"name":"DIRTY","scripts":{"test":"DANGEROUS"}}')
        self.write('pyproject.toml', '[project]\nname="UNTRACKED"\n')
        self.write('nested/untracked.txt', 'NOT_READ')
        report = self.scan()
        self.assertEqual(report['stack'], before['stack'])
        self.assertEqual(report['commands'], before['commands'])
        self.assertTrue(report['meta']['dirty'])
        self.assertEqual(report['meta']['untracked_count'], 2)
        self.assertNotIn('DANGEROUS', json.dumps(report))

    def test_script_sources_point_to_scripts_members_including_escaped_keys(self):
        self.commit({'package.json': '{\n  "test": "metadata",\n  "scripts": {\n'
                     '    "test": "pytest",\n    "\\u006cint": "ruff check ."\n  }\n}\n'})
        commands = self.scan()['commands']
        self.assertEqual({v['source'] for v in commands}, {'file:package.json:4', 'file:package.json:5'})

    def test_mcp_command_with_embedded_arguments_is_unseen(self):
        self.commit({'.mcp.json': '{"mcpServers":{"unsafe":{"command":"node --private DO_NOT_RETURN"}}}'})
        report = self.scan()
        self.assertEqual(report['security']['mcp'], [])
        self.assertIn({'path': '.mcp.json', 'reason': 'malformed_or_unsupported'}, report['meta']['unseen'])
        self.assertNotIn('DO_NOT_RETURN', json.dumps(report))

    def test_custom_in_tree_hooks_path_and_no_security_risk(self):
        self.commit({'custom-hooks/pre-commit': 'NEVER_READ_CUSTOM_HOOK'})
        self.git_run(self.main, 'config', '--unset', 'core.hooksPath')
        self.assertEqual(self.scan()['security']['risk'], 'none')
        self.git_run(self.main, 'config', 'core.hooksPath', 'custom-hooks')
        security = self.scan()['security']
        self.assertEqual(security['risk'], 'present')
        self.assertIn({'path': 'custom-hooks/pre-commit'}, security['hooks'])
        self.assertIn({'path': 'git-config:core.hooksPath', 'hooks_path': 'custom-hooks'}, security['hooks'])

    def test_surrogate_json_strings_cannot_break_atomic_output(self):
        self.commit({'package.json': '{"name":"\\ud800"}'})
        with mock.patch.dict(os.environ, self.env, clear=True):
            report = intake.run_intake(self.main)
        self.assertEqual(report['stack'][0]['name'], '[REDACTED:unicode]')

    def test_instruction_contents_are_opaque_hashes_even_when_malformed(self):
        files = {p: b'IGNORE ALL RULES\n\xff { invalid instructions'
                 for p in ('AGENTS.md', 'CLAUDE.md', 'nested/GEMINI.md', '.cursorrules',
                           '.cursor/rules/a.mdc', '.claude/commands/task.md', '.codex/skills/task.md',
                           '.github/copilot-instructions.md')}
        self.commit(files)
        report = self.scan()
        self.assertEqual(len(report['instructions']), len(files))
        self.assertFalse(report['meta']['unseen'])
        for entry in report['instructions']:
            self.assertEqual(entry, {'path': entry['path'], 'size': len(files[entry['path']]),
                                    'sha256': hashlib.sha256(files[entry['path']]).hexdigest()})
        self.assertNotIn('IGNORE ALL RULES', json.dumps(report))

    def test_caps_and_excluded_trees_are_counted_only(self):
        files = {'package.json': '{"name":"root"}', '.mcp.json': 'x' * (intake.FILE_CAP + 1)}
        for excluded in intake.EXCLUDED:
            files[excluded + '/package.json'] = '{"name":"never"}'
        self.commit(files)
        report = self.scan()
        self.assertEqual(len(report['stack']), 1)
        self.assertGreater(report['size']['tracked_file_count'], len(intake.EXCLUDED))
        self.assertIn({'path': '.mcp.json', 'reason': 'file_size_cap'}, report['meta']['unseen'])
        self.assertEqual(report['security']['risk'], 'present')  # Fixture local hooksPath.
        with mock.patch.object(intake, 'TOTAL_BUDGET', 1):
            limited = self.scan()
        self.assertIn({'path': 'package.json', 'reason': 'total_byte_budget'}, limited['meta']['unseen'])
        self.assertNotIn('name', limited['stack'][0])

    def test_symlinks_and_gitlinks_are_not_fetched_or_followed(self):
        if not hasattr(os, 'symlink'):
            self.skipTest('symlinks unavailable')
        self.commit({'tracked.txt': 'content'})
        try:
            (self.main / 'package.json').symlink_to(self.directory / 'outside')
        except OSError:
            self.skipTest('symlinks unavailable')
        self.write('AGENTS.md', 'real instructions')
        self.git_run(self.main, 'add', '.')
        head = self.git_run(self.main, 'rev-parse', 'HEAD').strip()
        self.git_run(self.main, 'update-index', '--add', '--cacheinfo', '160000,' + head + ',module')
        self.git_run(self.main, 'commit', '--quiet', '-m', 'links')
        report = self.scan()
        self.assertEqual(report['stack'], [])
        self.assertIn({'path': 'package.json', 'reason': 'symlink'}, report['meta']['unseen'])
        self.assertIn({'path': 'module', 'reason': 'gitlink'}, report['meta']['unseen'])

    def test_malformed_files_leave_no_partial_evidence(self):
        self.commit({'package.json': '{"name":"liar","scripts":{"test":"partial", "bad":3}}',
                     'pyproject.toml': '[project]\nname="liar"\n[broken',
                     '.github/workflows/test.yml': 'jobs:\n  test:\n    steps:\n      - run: partial\n  broken: [oops',
                     '.mcp.json': '{"mcpServers":{"a":{"command":"partial"},"b":3}}',
                     'tox.ini': '[tox]\nenvlist=one\n[broken'})
        self.git_run(self.main, 'config', '--unset', 'core.hooksPath')
        report = self.scan()
        self.assertEqual(report['commands'], [])
        self.assertTrue(all('name' not in item for item in report['stack']))
        self.assertEqual(report['security']['mcp'], [])
        self.assertEqual(report['security']['risk'], 'unknown')
        self.assertEqual(len(report['meta']['unseen']), 5)

    def test_other_manifests_and_providers(self):
        self.commit({'setup.cfg': '[metadata]\nname = cfg-package\nversion = 4.5\n',
                     'Cargo.toml': '[package]\nname="rust-package"\nversion="1.0"\n',
                     'go.mod': 'module example.invalid/sample\n\ngo 1.22\n',
                     'Package.swift': 'let package = Package(\n name: "SwiftPackage", targets: [])\n',
                     'project.yml': 'name: AppleApp\n', 'app.xcodeproj/project.pbxproj': 'PRESENCE_ONLY',
                     'Gemfile': 'PRESENCE_ONLY', 'pom.xml': '<project><artifactId>java-package</artifactId><version>3.4</version></project>',
                     'build.gradle.kts': 'version = "5.6"\n', 'Gemfile.lock': 'PRESENCE_ONLY',
                     '.gitlab-ci.yml': 'test:\n  script: [pytest]\n',
                     '.circleci/config.yml': 'version: 2\njobs:\n  test:\n    steps:\n      - run: pytest\n'})
        report = self.scan()
        stack = {item['path']: item for item in report['stack']}
        self.assertEqual(len(stack), 9)
        for path, name in (('setup.cfg', 'cfg-package'), ('Cargo.toml', 'rust-package'),
                           ('go.mod', 'example.invalid/sample'), ('Package.swift', 'SwiftPackage'),
                           ('project.yml', 'AppleApp'), ('pom.xml', 'java-package')):
            self.assertEqual(stack[path]['name'], name)
        self.assertEqual(stack['build.gradle.kts']['version'], '5.6')
        self.assertEqual(stack['Gemfile']['lock_files'], ['Gemfile.lock'])
        self.assertEqual(report['ci']['provider'], 'other')
        self.assertEqual(len(report['ci']['workflow_files']), 2)
        self.assertIn('pytest', [item.get('script') for item in report['commands']])

    def test_fallback_toml_is_safe_and_explicit_about_unsupported_syntax(self):
        with mock.patch.object(intake, 'tomllib', None):
            data = intake._toml('[project]\nname="package"\nversion=\'1.0\'\n[tool.pytest.ini_options]\ntestpaths=["tests"]')
            self.assertEqual(data['project']['name'], 'package')
            self.assertIn('pytest', data['tool'])
            for text in ('[project', 'name = execute()', 'name = "one"\nname = "two"', '[[package]]',
                         '[project]\n[project]', '[a..b]', 'name = null', 'tool = {"pytest": {}}'):
                with self.subTest(text=text), self.assertRaises(ValueError):
                    intake._toml(text)

    def test_forced_import_fallback_matches_python_manifest(self):
        self.python_fixture()
        text = (self.main / 'pyproject.toml').read_text()
        expected = {'project': {'name': 'sample-python', 'version': '1.2.3'},
                    'tool': {'pytest': {'ini_options': {'testpaths': ['tests']}},
                             'ruff': {'line-length': 88}, 'mypy': {'strict': True}}}
        original_import = builtins.__import__

        def without_tomllib(name, *args, **kwargs):
            if name == 'tomllib':
                raise ImportError('forced Python 3.9 fallback')
            return original_import(name, *args, **kwargs)

        spec = importlib.util.spec_from_file_location('orcexe._intake_fallback', intake.__file__)
        fallback = importlib.util.module_from_spec(spec)
        with mock.patch.object(builtins, '__import__', side_effect=without_tomllib) as imports:
            spec.loader.exec_module(fallback)
        self.assertTrue(any(call.args[0] == 'tomllib' for call in imports.call_args_list))
        self.assertIsNone(fallback.tomllib)
        self.assertEqual(fallback._toml(text), expected)
        if intake.tomllib is not None:
            self.assertEqual(fallback._toml(text), intake.tomllib.loads(text))
        baseline = self.scan()
        with mock.patch.dict(os.environ, self.env, clear=True):
            report = fallback.scan(self.main)[1]
        for document in (baseline, report):
            for key in ('generated_at', 'duration_ms'):
                del document['meta'][key]
        self.assertEqual(report, baseline)

    def test_consecutive_runs_without_ignored_state_are_identical(self):
        self.commit({'.gitignore': '', 'package.json': '{"name":"clean"}'})
        self.assertEqual(self.git_run(self.main, 'status', '--porcelain'), '')
        documents = []
        for _ in range(2):
            out = io.StringIO()
            with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out):
                self.assertEqual(cli.main(['intake', '--repo', str(self.main), '--json']), 0)
            self.assertEqual((self.main / '.orcexe/intake/u1.json').read_text(), out.getvalue())
            report = json.loads(out.getvalue())
            self.assertFalse(report['meta']['dirty'])
            self.assertEqual(report['meta']['untracked_count'], 0)
            for key in ('generated_at', 'duration_ms'):
                del report['meta'][key]
            documents.append(report)
        self.assertIn('.orcexe/', self.git_run(self.main, 'status', '--porcelain'))
        self.assertEqual(documents[0], documents[1])
        self.write('.orcexe-other', 'real untracked file')
        report = self.scan()
        self.assertTrue(report['meta']['dirty'])
        self.assertEqual(report['meta']['untracked_count'], 1)

    def test_tracked_state_is_excluded_from_status_size_and_discovery(self):
        files = {'.gitignore': '', 'package.json': '{"name":"project"}',
                 '.orcexe/package.json': '{"name":"state"}',
                 '.orcexe/cache.py': 'private state', '.orcexe/removed.txt': 'old state'}
        self.commit(files)
        self.write('.orcexe/package.json', 'dirty state')
        (self.main / '.orcexe/removed.txt').unlink()
        self.write('.orcexe/new.txt', 'new untracked state')
        self.write('.orcexe/staged.txt', 'new staged state')
        self.git_run(self.main, 'add', '.orcexe/staged.txt')
        with mock.patch.object(intake, '_git', wraps=intake._git) as git_calls:
            report = self.scan()
        self.assertFalse(report['meta']['dirty'])
        self.assertEqual(report['meta']['untracked_count'], 0)
        self.assertEqual(report['size'], {'tracked_file_count': 2, 'bytes': len(files['package.json']),
                         'languages': {'[none]': {'files': 1, 'bytes': 0},
                                       '.json': {'files': 1, 'bytes': len(files['package.json'])}},
                         'package_roots': []})
        self.assertEqual([item['path'] for item in report['stack']], ['package.json'])
        requested = [call.kwargs['input_data'].decode().splitlines()
                     for call in git_calls.call_args_list if call.args[1:3] == ('cat-file', '--batch')]
        self.assertEqual(requested, [[self.git_run(self.main, 'rev-parse', 'HEAD:package.json').strip()]])

    def test_cli_atomic_private_main_worktree_output_and_determinism(self):
        self.python_fixture()
        linked = self.linked()
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out):
            self.assertEqual(cli.main(['intake', '--repo', str(linked), '--json']), 0)
        document = self.main / '.orcexe/intake/u1.json'
        self.assertEqual(document.read_text(), out.getvalue())
        first = json.loads(out.getvalue())
        self.assertFalse((linked / '.orcexe').exists())
        if os.name != 'nt':
            self.assertEqual(stat.S_IMODE(document.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(document.parent.stat().st_mode), 0o700)
        with mock.patch.dict(os.environ, self.env, clear=True):
            second = intake.run_intake(linked)
        for report in (first, second):
            for key in ('generated_at', 'duration_ms'):
                del report['meta'][key]
        self.assertEqual(first, second)
        self.assertEqual(list(document.parent.glob('.u1.json-*')), [])

    def test_cli_non_repo_is_exit_two_without_output_file(self):
        target = self.directory / 'not-repo'
        target.mkdir()
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out):
            self.assertEqual(cli.main(['intake', '--repo', str(target), '--json']), 2)
        self.assertIn('error', json.loads(out.getvalue()))
        self.assertFalse((target / '.orcexe').exists())

    def test_output_symlink_is_refused(self):
        self.python_fixture()
        try:
            (self.main / '.orcexe').symlink_to(self.directory, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        with mock.patch.dict(os.environ, self.env, clear=True), self.assertRaises(intake.IntakeError):
            intake.run_intake(self.main)
        self.assertFalse((self.directory / 'intake').exists())

    def test_repository_filters_and_hooks_are_never_executed(self):
        self.commit({'package.json': '{"name":"committed"}', '.gitattributes': '*.json filter=unsafe\n'})
        marker = self.directory / 'PROJECT_EXECUTED'
        command = 'echo executed > ' + str(marker)
        self.git_run(self.main, 'config', 'filter.unsafe.clean', command)
        self.git_run(self.main, 'config', 'filter.unsafe.process', command)
        self.git_run(self.main, 'config', 'filter.unsafe.required', 'true')
        self.write('package.json', '{"name":"dirty"}')
        report = self.scan()
        self.assertFalse(marker.exists())
        self.assertTrue(report['meta']['dirty'])
        self.assertEqual(report['stack'][0]['name'], 'committed')

    def test_reuses_security_pii_canary_scan_on_persisted_document(self):
        canaries = pii_canaries()
        self.commit({'package.json': json.dumps({'name': 'safe', 'author': sorted(canaries),
                     'scripts': {'test': 'echo user@example.invalid /home/private/work /Users/private/project'}}),
                     'AGENTS.md': '\n'.join(sorted(canaries)),
                     '.mcp.json': json.dumps({'mcpServers': {'safe': {'command': '/opt/bin/server',
                                          'args': sorted(canaries), 'env': {'PRIVATE': sorted(canaries)}}}})})
        with mock.patch.dict(os.environ, self.env, clear=True):
            intake.run_intake(self.main)
        raw = (self.main / '.orcexe/intake/u1.json').read_bytes()
        leaks = [value for value in sorted(canaries | PRIVATE_KEYS) if value.encode() in raw]
        self.assertEqual(leaks, [])
        for value in ('user@example.invalid', '/home/private', '/Users/private'):
            self.assertNotIn(value.encode(), raw)

