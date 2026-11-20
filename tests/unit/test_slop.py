"""Offline corpus tests: source syntax is evidence, never executable input."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time
from unittest import mock, skipUnless

from orcexe import cli, intake, slop
from tests.unit.test_gitutil import GitFixture, ROOT
from tests.unit._timing import scaled
from tests.security import capture_children


class SlopTests(GitFixture):
    def write(self, path, text):
        target = self.main / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text if isinstance(text, bytes) else text.encode())

    def commit(self, files):
        self.write('.gitignore', '.orcexe/\n')
        for path, text in files.items():
            self.write(path, text)
        self.git_run(self.main, 'add', '.')
        self.git_run(self.main, 'commit', '--quiet', '-m', 'synthetic slop fixture')

    def scan(self, repo=None):
        with mock.patch.dict(os.environ, self.env, clear=True):
            return slop.scan(repo or self.main)[1]

    def findings(self, kind):
        return [v for v in self.scan()['findings'] if v['class'] == kind]

    def test_python_assertion_forms_and_constants(self):
        self.commit({'test_suite.py': '''
def test_no_assert():
    product()
def test_constant():
    assert 1 == 1
def test_real():
    assert product() == 1
def test_framework():
    self.assertEqual(product(), 1)
def test_exception():
    with pytest.raises(ValueError):
        product()
def test_unittest_exception():
    with self.assertRaises(ValueError):
        product()
def test_mock():
    product.assert_called_once()
def test_nested():
    def helper():
        assert product()
'''})
        self.assertEqual([v['line'] for v in self.findings('fake_test')], [2, 4, 18])

    def test_js_ts_assertion_forms_and_constants(self):
        source = '''test('empty', () => { product(); });
it('constant', () => { expect(true).toBe(true); });
it('product', () => { expect(product()).toEqual(1); });
it('reverse', () => { expect(1).toEqual(product()); });
it('throws', () => { expect(() => product()).toThrow(); });
it('node', () => { assert.strictEqual(product(), 1); });
it('constant node', () => { assert.equal(1, 1); });
it.each([1, 2])('data', value => { expect(value).toBe(1); });
'''
        self.commit({'suite.test.ts': source, 'suite.test.js': source})
        findings = self.findings('fake_test')
        self.assertEqual([(v['path'], v['line']) for v in findings],
                         [(path, line) for path in ('suite.test.js', 'suite.test.ts') for line in (1, 2, 7)])

    def test_skips_markers_calls_and_comments(self):
        self.commit({'test_a.py': '''@pytest.mark.skip(reason="later")
def test_skip():
    assert product()
@pytest.mark.xfail
def test_xfail():
    pytest.skip("later")
    assert product()
# def test_removed():
''', 'test_a.ts': '''test.skip('a', () => { expect(product()).toBe(1); });
fit('a', () => { expect(product()).toBe(1); });
it.only('a', () => { expect(product()).toBe(1); });
// test('removed', () => {});
''', 'A.swift': '''func testA() throws {
    throw XCTSkip("later")
}
// func testRemoved() {}
'''})
        findings = self.findings('skipped_test')
        self.assertEqual([(v['path'], v['line']) for v in findings],
                         [('A.swift', 2), ('A.swift', 4), ('test_a.py', 1), ('test_a.py', 4),
                          ('test_a.py', 6), ('test_a.py', 8), ('test_a.ts', 1), ('test_a.ts', 2),
                          ('test_a.ts', 3), ('test_a.ts', 4)])

    def test_suppressions_only_in_comments(self):
        self.commit({'a.py': '''value = 1  # type: ignore
# pylint: disable=all
# pragma: no cover
value = "# noqa"
''', 'a.ts': '''// eslint-disable-next-line
/* @ts-nocheck */
/* istanbul ignore next */
const s = '// eslint-disable';
''', 'A.swift': '''// swiftlint:disable all
let s = "// swiftlint:disable all"
'''})
        findings = self.findings('suppression')
        self.assertEqual(len(findings), 7)
        self.assertEqual({v['language'] for v in findings}, {'python', 'typescript', 'swift'})

    def test_stubs_and_required_exemptions(self):
        self.commit({'a.py': '''from typing import Protocol, overload
from abc import ABC, abstractmethod
class Contract(Protocol):
    def method(self): ...
class Abstract(ABC):
    @abstractmethod
    def method(self):
        raise NotImplementedError
    def concrete(self):
        raise NotImplementedError
@overload
def typed(x: int): ...
def missing():
    raise NotImplementedError("later")
def todo():
    # TODO implement this
    pass
def valid():
    pass
''', 'a.ts': '''interface Contract { method(): void; }
export function missing() { throw new Error("Not implemented"); }
function todo() { /* FIXME implement this */ }
function valid() {}
function guard() { throw new Error("invalid input"); }
''', 'A.swift': '''protocol Contract { func method() }
func missing() { fatalError("Not implemented") }
func todo() { /* TODO implement this */ }
func guardValue() { preconditionFailure("not implemented") }
func guardOther() { guard good else { fatalError("bad input") } }
'''})
        findings = self.findings('stub')
        self.assertEqual([(v['path'], v['line']) for v in findings],
                         [('A.swift', 2), ('A.swift', 3), ('a.py', 10), ('a.py', 14), ('a.py', 17),
                          ('a.ts', 2), ('a.ts', 3)])

    def test_string_template_regex_and_docstring_immunity(self):
        self.commit({'a.py': '''"""# noqa
# def test_removed():
def fake(): raise NotImplementedError
"""
value = "pytest.skip('later')"
''', 'a.ts': '''const a = "test.skip('x', () => {}) // eslint-disable";
const b = `/* @ts-ignore */ ${"it.only()"} ${`nested test.skip()`}`;
const c = /test.skip\(.*\/\/ eslint-disable/;
''', 'A.swift': '''let a = "XCTSkip() fatalError(\\"not implemented\\")"
let b = """
// swiftlint:disable all
func testRemoved() { fatalError("not implemented") }
"""
'''})
        report = self.scan()
        self.assertFalse([v for v in report['findings'] if v['class'] in
                          ('skipped_test', 'suppression', 'stub', 'fake_test')])
        self.assertFalse([v for v in report['unseen'] if v.get('reason') == 'malformed_or_unsupported'])

    def test_python_documented_placeholders(self):
        self.commit({'a.py': '''def pending_pass():
    """TODO: implement this."""
    pass
def pending_ellipsis():
    """FixMe: not implemented."""
    ...
async def pending_return():
    """XXX: stub."""
    return
class Concrete:
    def pending_none(self):
        """@todo: implement this."""
        return None
def pending_empty():
    """TODO:
    implement this.
    """
def comment_return():
    # XXX: stub
    return None
'''})
        self.assertEqual([(v['line'], v['signal']) for v in self.findings('stub')],
                         [(3, 'todo_body'), (6, 'todo_body'), (9, 'todo_body'),
                          (13, 'todo_body'), (14, 'todo_body'), (20, 'todo_body')])

    def test_python_documentation_ownership_and_exclusions(self):
        self.commit({'a.py': '''"""TODO: implement module."""
from abc import abstractmethod
from typing import Protocol, overload
def prose():
    """Implementation is not implemented yet; this is a stub."""
    pass
def unrelated():
    """TODO: document examples."""
    return None
def implemented():
    """TODO: implement a faster version."""
    return product()
def other_string():
    pass
    "TODO: implement this"
def outer():
    """TODO: implement this."""
    def inner():
        pass
class Documented:
    """TODO: implement this."""
    def method(self):
        pass
class Contract(Protocol):
    def method(self):
        """TODO: implement this."""
        return None
class Abstract:
    @abstractmethod
    def method(self):
        """FIXME: not implemented."""
        pass
@overload
def typed(value: int):
    """XXX: stub."""
    ...
'''})
        self.assertEqual(self.findings('stub'), [])

    def test_js_ts_documented_placeholders(self):
        source = '''/** @todo implement this */
export function pending() {
}
class Concrete {
    /** FIXME: not implemented */
    method() {
        return;
    }
}
/** XXX: stub */
const arrow = () => {
    return null;
};
function inside() {
    /** @todo
     * implement this
     */
    return;
}
/** TODO: implement this */
async function asynchronous()
{
}
/** TODO: implement this */
const single = value => { return undefined; };
consume(/** TODO: implement this */ () => { return; });
'''
        self.commit({'a.js': source, 'a.ts': source})
        self.assertEqual([(v['path'], v['line'], v['signal']) for v in self.findings('stub')],
                         [(path, line, 'todo_body') for path in ('a.js', 'a.ts')
                          for line in (2, 7, 12, 18, 21, 25, 26)])

    def test_js_ts_documentation_ownership_and_exclusions(self):
        source = '''/** Implementation is not implemented yet; this is a stub. */
function prose() {}
/** @todo document examples */
function unrelated() { return; }
/** TODO: implement a faster version */
function implemented() { return product(); }
/** TODO: implement this */
const value = 1;
function detached() {}
/** TODO: implement this */
class Documented { method() {} }
function literals() { "TODO: implement this"; }
/** TODO: implement this */
function outer() { function inner() {} }
/** TODO: implement this */
if (ready) {}
'''
        self.commit({'a.js': source, 'a.ts': source + '''interface Contract {
    /** TODO: implement this */
    method(): void;
}
abstract class Abstract {
    /** TODO: implement this */
    abstract method(): void;
}
/** TODO: implement this */
function overloaded(value: string): void;
function overloaded(value: unknown) { product(value); }
'''})
        self.assertEqual(self.findings('stub'), [])

    def test_documented_stub_recount_changes_no_other_findings(self):
        source = '''def pending():
    """TODO: implement this."""
    pass
'''
        self.commit({'a.py': source, 'a.ts': '/** TODO: implement this */\nfunction pending() {}\n'})
        before = self.scan()
        for path in ('a.py', 'a.ts'):
            with self.subTest(path=path):
                self.commit({path: (self.main / path).read_text().replace('TODO:', 'Note:')})
                after = self.scan()
                self.assertEqual(before['counts']['stub'] - after['counts']['stub'], 1)
                removed = next(v for v in before['findings'] if v['class'] == 'stub' and v['path'] == path)
                self.assertEqual([v for v in before['findings'] if v != removed], after['findings'])
                before = after

    def test_advisory_dead_exports_and_other_file_references(self):
        self.commit({'a.py': 'def public(): return 1\ndef used(): return 1\ndef _private(): return 1\n',
                     'b.py': 'from a import used\nused()\n',
                     'a.ts': 'export function publicTs() {}\nexport const usedTs = 1;\n',
                     'b.ts': 'import { usedTs } from "./a";\nconsole.log(usedTs);\n'})
        findings = self.findings('dead_code')
        self.assertEqual([v['signal'] for v in findings],
                         ['unreferenced_export:public', 'unreferenced_export:publicTs'])
        self.assertTrue(all(v['advisory'] for v in findings))

    def test_python_dependency_manifests_aliases_stdlib_and_first_party(self):
        self.commit({'pyproject.toml': '''[project]
name = "local-product"
dependencies = [
 "PyYAML>=6",
 "attrs", "python-dateutil", "Pillow", "scikit-learn", "beautifulsoup4", "unused-runtime"
]
[project.optional-dependencies]
test = ["pytest"]
''', 'src/local_product/__init__.py': '',
                     'src/local_product/app.py': '''import os, sys, pathlib, yaml, attrs
from dateutil import parser
from PIL import Image
import sklearn, bs4, pytest, local_product
from . import relative
import undeclared
''', 'nested/requirements.txt': 'requests>=2\n',
                     'nested/requirements-dev.txt': 'ruff\n',
                     'nested/app.py': 'import requests, ruff\n',
                     'cfg/setup.cfg': '[metadata]\nname = cfg\n[options]\ninstall_requires =\n    click>=8\n',
                     'cfg/main.py': 'import click\n'})
        findings = self.findings('dependency')
        self.assertEqual([(v['path'], v['line'], v['signal']) for v in findings],
                         [('pyproject.toml', 0, 'unused_runtime:unused-runtime'),
                          ('src/local_product/app.py', 6, 'undeclared_import')])

    def test_node_dependency_subpaths_scopes_nearest_and_unused(self):
        self.commit({'package.json': json.dumps({'name': 'local-name', 'dependencies': {
            'lodash': '*', '@scope/pkg': '*', 'unused': '*'}, 'devDependencies': {'vitest': '*'},
            'peerDependencies': {'react': '*'}}),
            'a.ts': '''import {x} from 'lodash/map';
import y from '@scope/pkg/sub';
import 'vitest';
import 'react';
const fs = require('node:fs/promises');
import path from 'path';
import local from './local';
import own from 'local-name';
import missing from 'undeclared';
''', 'nested/package.json': '{"dependencies":{"child":"*"}}',
            'nested/a.js': "import 'child';\nimport 'lodash';\n"})
        findings = self.findings('dependency')
        self.assertEqual([(v['path'], v['line'], v['signal']) for v in findings],
                         [('a.ts', 9, 'undeclared_import'), ('nested/a.js', 2, 'undeclared_import'),
                          ('package.json', 0, 'unused_runtime:unused')])

    def test_missing_checks_uses_intake_candidates(self):
        self.commit({'a.py': 'value = 1\n'})
        self.assertEqual({v['signal'] for v in self.findings('missing_checks')}, {'no_test', 'no_lint', 'no_ci'})
        self.commit({'Makefile': 'test:\n\tfalse\nlint:\n\tfalse\n',
                     '.github/workflows/check.yml': 'jobs:\n  check:\n    steps:\n      - run: never-execute\n'})
        self.assertEqual(self.findings('missing_checks'), [])

    def test_recount_fix_one_changes_no_other_findings(self):
        self.commit({'test_a.py': 'def test_a():\n    assert 1 == 1\ndef test_b():\n    assert True\n'})
        before = self.scan()
        self.commit({'test_a.py': 'def test_a():\n    assert product() == 1\ndef test_b():\n    assert True\n'})
        after = self.scan()
        self.assertEqual(before['counts']['fake_test'] - after['counts']['fake_test'], 1)
        removed = dict(before['findings'][next(i for i, v in enumerate(before['findings'])
                                              if v['class'] == 'fake_test')])
        self.assertEqual([v for v in before['findings'] if v != removed], after['findings'])

    def test_head_only_vendored_secret_oid_self_exclusion_and_caps(self):
        files = {'a.py': '# noqa\n', '.env/private.py': '# PRIVATE_SECRET\n',
                 'copy.py': '# PRIVATE_SECRET\n', '.orcexe/state.py': '# noqa\n',
                 'large.py': 'a' * (intake.FILE_CAP + 1), 'other.go': '// never scanned\n'}
        for path in intake.EXCLUDED:
            files[path + '/a.py'] = '# noqa\n'
        self.commit(files)
        self.write('a.py', 'clean = 1\n')
        self.write('untracked.py', '# noqa\n')
        original = intake._git
        requests = []

        def observe(repo, *args, **kwargs):
            if args[:2] == ('cat-file', '--batch'):
                requests.extend(kwargs['input_data'].decode().splitlines())
            return original(repo, *args, **kwargs)
        with mock.patch.object(intake, '_git', side_effect=observe):
            report = self.scan()
        self.assertEqual([v['path'] for v in report['findings'] if v['class'] == 'suppression'], ['a.py'])
        secret = self.git_run(self.main, 'rev-parse', 'HEAD:.env/private.py').strip()
        self.assertNotIn(secret, requests)
        for item in ({'path': 'copy.py', 'reason': 'secret_blob'},
                     {'path': 'large.py', 'reason': 'file_size_cap'},
                     {'extension': '.go', 'reason': 'unsupported_extension', 'count': 1}):
            self.assertIn(item, report['unseen'])
        self.assertNotIn('PRIVATE_SECRET', json.dumps(report))
        with mock.patch.object(intake, 'TOTAL_BUDGET', 1):
            self.assertIn({'path': 'a.py', 'reason': 'total_byte_budget'}, self.scan()['unseen'])

    def test_cli_private_atomic_output_determinism_and_markdown(self):
        self.commit({'a.py': '# noqa\ndef public(): raise NotImplementedError\n'})
        linked = self.linked()
        documents = []
        for _ in range(2):
            out = io.StringIO()
            with mock.patch.dict(os.environ, self.env, clear=True), redirect_stdout(out):
                self.assertEqual(cli.main(['slop', '--repo', str(linked), '--json']), 0)
            saved = self.main / '.orcexe/slop.json'
            self.assertEqual(saved.read_text(), out.getvalue())
            document = json.loads(out.getvalue())
            documents.append({k: v for k, v in document.items() if k != 'meta'})
            self.assertEqual(document['schema'], 'orcexe.slop.v1')
            self.assertEqual(document['findings'], sorted(document['findings'],
                             key=lambda v: (v['class'], v['path'], v['line'], v['signal'])))
        self.assertEqual(documents[0], documents[1])
        self.assertFalse((linked / '.orcexe').exists())
        markdown = self.main / '.orcexe/slop-report.md'
        self.assertIn('a.py:1', markdown.read_text())
        self.assertIn('lane: fast', markdown.read_text())
        self.assertIn('advisory', markdown.read_text())
        if os.name != 'nt':
            for path in (saved, markdown):
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(list(saved.parent.glob('.slop*')), [])

    def test_nonrepo_unborn_malformed_and_output_symlink(self):
        target = self.directory / 'nonrepo'
        target.mkdir()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['slop', '--repo', str(target)]), 2)
        self.assertIsNone(self.scan()['head'])
        self.commit({'broken.py': 'def invalid(\n', 'broken.ts': 'const s = `unterminated',
                     'package.json': '{invalid'})
        self.assertEqual(sum(v.get('reason') == 'malformed_or_unsupported' for v in self.scan()['unseen']), 3)
        try:
            (self.main / '.orcexe').symlink_to(self.directory, target_is_directory=True)
        except OSError:
            self.skipTest('symlinks unavailable')
        with self.assertRaises(intake.IntakeError):
            slop.run_slop(self.main)
        self.assertFalse((self.directory / 'slop.json').exists())

    def test_evidence_is_redacted_bounded_and_markdown_escaped(self):
        self.commit({'a.py': '# noqa user@example.invalid /home/private/repo <script> [link](url) ' + 'x' * 200})
        report = self.scan()
        finding = next(v for v in report['findings'] if v['class'] == 'suppression')
        self.assertLessEqual(len(finding['evidence']), 160)
        self.assertNotIn('user@example.invalid', finding['evidence'])
        self.assertNotIn('/home/private', finding['evidence'])
        self.assertNotIn('<script>', slop._markdown(report))
        self.assertNotIn('[link]', slop._markdown(report))

    def test_python39_import_and_repo_scan_budget(self):
        # Use the runner's interpreter, including Python 3.9 in compatibility CI.
        # Reaped child CPU excludes time waiting for a busy machine to schedule us.
        if os.name == 'posix':
            import resource
            before = resource.getrusage(resource.RUSAGE_CHILDREN)
        result = subprocess.run(
            [sys.executable, '-c',
             "import sys; sys.path.insert(0, 'plugins/orcexe/src'); "
             "import json; from pathlib import Path; from orcexe import slop; "
             "print(json.dumps(slop.scan(Path.cwd())[1]))"],
            cwd=ROOT, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True)
        if os.name == 'posix':
            after = resource.getrusage(resource.RUSAGE_CHILDREN)
            cpu_seconds = (after.ru_utime - before.ru_utime
                           + after.ru_stime - before.ru_stime)
        report = json.loads(result.stdout)
        self.assertEqual(set(report['counts']), set(slop.CLASSES))
        if os.name != 'posix':
            self.skipTest('scan/import verified; child CPU budget requires POSIX resource accounting')
        self.assertLess(cpu_seconds, scaled(10))

    @skipUnless(os.environ.get('ORCEXE_SLOW_TESTS') == '1',
                'wall-clock scan budget requires ORCEXE_SLOW_TESTS=1')
    def test_repository_scan_wall_clock_budget(self):
        started = time.monotonic()
        report = slop.scan(ROOT)[1]
        self.assertLess(time.monotonic() - started, scaled(10))
        self.assertLess(report['meta']['duration_ms'], scaled(10000))

    def test_subdirectory_scope_and_only_git_children(self):
        self.commit({'a.py': '# noqa\n', 'nested/b.py': '# noqa\n',
                     'package.json': '{"scripts":{"test":"never-run-test", "lint":"never-run-lint"}}'})
        expected = self.scan()
        with mock.patch.dict(os.environ, dict(self.env, ORCEXE_BIN_CODEX='/never/codex',
                             ORCEXE_BIN_CLAUDE_CODE='/never/claude', ORCEXE_BIN_CURSOR='/never/agent'), clear=True):
            with capture_children() as children:
                report = slop.run_slop(self.main / 'nested')
        self.assertEqual(report['findings'], expected['findings'])
        self.assertEqual({v['path'] for v in report['findings'] if v['class'] == 'suppression'},
                         {'a.py', 'nested/b.py'})
        self.assertTrue(children)
        self.assertTrue(all(Path(v['argv'][0]).stem in ('git', 'git-wrapper') for v in children), children)

    def test_additional_js_framework_assertions_and_export_forms(self):
        self.commit({'test_a.js': '''it('node matcher', () => { assert.match(product(), /ok/); });
it('node raises', () => { assert.doesNotThrow(() => product()); });
it('tap', () => { t.deepEqual(product(), 1); });
it('bare expect', () => { expect(product()); });
it('counter', () => { expect.assertions(1); });
it('expression', () => expect({value: product()}).toEqual({value: 1}));
''', 'a.ts': 'function publicName() {}\nexport { publicName };\nexport type PublicType = string;\n'})
        self.assertEqual([v['line'] for v in self.findings('fake_test')], [4, 5])
        self.assertEqual({v['signal'] for v in self.findings('dead_code')},
                         {'unreferenced_export:publicName', 'unreferenced_export:PublicType'})

    def test_class_skip_unittest_names_and_generic_protocol(self):
        self.commit({'test_a.py': '''from unittest import TestCase, skip
from typing import Protocol, TypeVar
T = TypeVar('T')
@skip('later')
class Disabled(TestCase):
    def testCamelCase(self):
        pass
class Contract(Protocol[T]):
    def missing(self):
        raise NotImplementedError
''', 'A.swift': 'func guardValue() { guard ready else { fatalError("not implemented") } }\n',
                     'a.ts': '''test.concurrent('object constants', () => { expect({value: 1}).toEqual({value: 1}); });
test('interpolated', () => { expect(`${product()}`).toBe('ok'); });
'''})
        self.assertEqual([(v['path'], v['line']) for v in self.findings('fake_test')],
                         [('a.ts', 1), ('test_a.py', 6)])
        self.assertEqual([v['line'] for v in self.findings('skipped_test')], [4])
        self.assertEqual(self.findings('stub'), [])

    def test_python_helpers_local_inherited_nested_depth_and_cycles(self):
        source = '''def leaf(value):
    assert value == 1
def middle(value):
    leaf(value)
def outer(value):
    middle(value)
def too_far(value):
    outer(value)
def cycle_a():
    cycle_b()
def cycle_b():
    cycle_a()
def check_empty():
    pass
def constant_helper():
    assert True
def test_depth_three():
    outer(product())
def test_depth_four():
    too_far(product())
def test_cycle():
    cycle_a()
def test_resolved_empty():
    check_empty()
def test_constant_helper():
    constant_helper()
def test_unknown():
    verify_external(product())
def test_unknown_assert():
    assert_external()
def test_nested_called():
    def helper():
        assert product()
    helper()
def test_nested_unused():
    def helper():
        assert product()
class Base:
    def check(self, value):
        self.leaf(value)
    def leaf(self, value):
        self.assertEqual(value, 1)
class Derived(Base):
    def test_inherited(self):
        self.check(product())
class Other:
    def helper(self):
        assert product()
class Isolated:
    def test_unrelated_class(self):
        self.helper()
'''
        self.commit({'tests/test_helpers.py': source})
        expected = ('test_depth_four', 'test_cycle', 'test_resolved_empty',
                    'test_constant_helper', 'test_nested_unused', 'test_unrelated_class')
        self.assertEqual([v['line'] for v in self.findings('fake_test')],
                         [i for i, line in enumerate(source.splitlines(), 1)
                          if any(line.strip().startswith('def ' + name + '(') for name in expected)])

    def test_python_imported_support_conftest_and_product_boundaries(self):
        self.commit({'tests/conftest.py': 'def fixture_check(value):\n    assert value\n',
                     'tests/support.py': '''def leaf(value):
    assert value
def helper(value):
    leaf(value)
class Base:
    def signal_run(self):
        assert product()
''', 'app/product.py': '''def validate(value):
    assert value
def verify_product(value):
    assert value
''', 'tests/test_imports.py': '''from tests.support import helper as delegated, Base
from .support import helper as relative
import tests.support as support
from . import support as relative_module
import tests.support
from app.product import validate, verify_product
import app.product as product

def test_imported():
    delegated(product())
def test_relative():
    relative(product())
def test_namespace():
    support.helper(product())
def test_package_namespace():
    tests.support.helper(product())
def test_relative_module():
    relative_module.helper(product())
def test_conftest():
    fixture_check(product())
def test_product():
    validate(product())
def test_product_named_verify():
    verify_product(product())
def test_product_namespace():
    product.verify_product(product())
class Child(Base):
    def test_inherited_support(self):
        self.signal_run()
'''})
        self.assertEqual([v['evidence'] for v in self.findings('fake_test')],
                         ['def test_product():', 'def test_product_named_verify():', 'def test_product_namespace():'])

    def test_js_helpers_local_imported_depth_cycles_and_product_boundaries(self):
        source = '''import { delegated as helper, constantHelper } from './support';
import * as support from './support';
import { verifyProduct, assertOutput } from '../app/product';
const { delegated: required } = require('./support');
function leaf(value) { expect(value).toBe(1); }
const middle = (value: number) => { leaf(value); };
const outer = value => middle(value);
function tooFar(value) { outer(value); }
function cycleA() { cycleB(); }
function cycleB() { cycleA(); }
function checkEmpty() {}
test('local', () => { leaf(product()); });
test('depth three', () => { outer(product()); });
test('depth four', () => { tooFar(product()); });
test('imported', () => { helper(product()); });
test('namespace', () => { support.delegated(product()); });
test('required', () => { required(product()); });
test('constant helper', () => { constantHelper(); });
test('cycle', () => { cycleA(); });
test('resolved empty', () => { checkEmpty(); });
test('unknown', () => { ensureResult(product()); });
test('product', () => { verifyProduct(product()); });
test('product assert', () => { assertOutput(product()); });
test('nested unused', () => { function unused() { expect(product()).toBe(1); } });
test('nested called', () => { const local = () => { expect(product()).toBe(1); }; local(); });
test('unknown assert', () => { assertExternal(); });
'''
        self.commit({'tests/suite.test.ts': source,
                     'tests/support.ts': '''export const delegated = (value: number) => expect(value).toBe(1);
export function constantHelper() { expect(true).toBe(true); }
''', 'app/product.ts': '''export function verifyProduct(value) { expect(value).toBe(1); }
export function assertOutput(value) { expect(value).toBe(1); }
'''})
        self.assertEqual([v['line'] for v in self.findings('fake_test')], [14, 18, 19, 20, 22, 23, 24])

    def test_conditional_skips_are_advisory_and_unconditional_stay_scored(self):
        source = '''import os, sys, shutil, unittest, pytest
HAS_TOOL = shutil.which('tool') is not None
@pytest.mark.skipif(sys.platform == 'win32', reason='platform')
def test_platform():
    assert product()
@unittest.skipUnless(condition=HAS_TOOL, reason='dependency')
class Platform:
    def test_runtime(self):
        if os.environ.get('OPTION'):
            pytest.skip('environment')
        if not HAS_TOOL:
            self.skipTest('dependency')
        else:
            self.skipTest('other availability branch')
        assert product()
@unittest.skipIf(True, 'always')
@unittest.skipUnless(False, 'always')
@pytest.mark.skipif(1 == 1, reason='constant')
@pytest.mark.xfail(sys.platform == 'win32')
@pytest.mark.skip(reason='later')
def test_disabled():
    if True:
        pytest.skip('always')
    if broken_result:
        self.skipTest('hide failure')
    if os.name == 'posix':
        pytest.xfail('still scored')
    pytest.skip('unconditional')
    assert product()
# def test_commented():
'''
        self.commit({'tests/test_skips.py': source, 'A.swift': '''func testPlatform() throws {
    try XCTSkipIf(osUnavailable, "platform")
    try XCTSkipUnless(toolAvailable(), "tool")
    try XCTSkipIf(true, "always")
    try XCTSkipUnless(false, "always")
    throw XCTSkip("later")
}
'''})
        findings = self.findings('skipped_test')
        self.assertEqual([(v['path'], v['line']) for v in findings if v['advisory']],
                         [('A.swift', 2), ('A.swift', 3)] + [('tests/test_skips.py', n) for n in (3, 6, 10, 12, 14)])
        self.assertTrue(all(v['signal'] == 'conditional_skip' for v in findings if v['advisory']))
        self.assertEqual([v['line'] for v in findings if not v['advisory']],
                         [4, 5, 6, 16, 17, 18, 19, 20, 23, 25, 27, 28, 30])

    def test_helper_and_conditional_skip_recount_is_deterministic(self):
        self.commit({'tests/support.py': 'def helper(value):\n    assert value\n',
                     'tests/test_a.py': '''from tests.support import helper
import unittest, os
@unittest.skipIf(os.name == 'nt', 'platform')
def test_a():
    helper(product())
def test_b():
    product()
'''})
        before = self.scan()
        again = self.scan()
        self.assertEqual({k: v for k, v in before.items() if k != 'meta'},
                         {k: v for k, v in again.items() if k != 'meta'})
        for kind in slop.CLASSES:
            self.assertEqual(before['counts'][kind], sum(v['class'] == kind for v in before['findings']))
        self.commit({'tests/support.py': 'def helper(value):\n    product(value)\n'})
        after = self.scan()
        self.assertEqual(after['counts']['fake_test'], before['counts']['fake_test'] + 1)
        self.assertEqual([v for v in before['findings'] if v['class'] != 'fake_test'],
                         [v for v in after['findings'] if v['class'] != 'fake_test'])


    def test_conditional_skip_import_aliases(self):
        self.commit({'tests/test_aliases.py': """from unittest import skipIf as disabled_on
from pytest import skip as unavailable, xfail as expected_failure
from shutil import which as find_tool
@disabled_on(find_tool('tool') is None, 'missing tool')
def test_aliases():
    if find_tool('tool') is None:
        unavailable('missing tool')
        expected_failure('still scored')
    assert product()
"""})
        self.assertEqual([(v['line'], v['signal'], v['advisory']) for v in self.findings('skipped_test')],
                         [(4, 'conditional_skip', True), (7, 'conditional_skip', True), (8, 'runtime_skip', False)])
