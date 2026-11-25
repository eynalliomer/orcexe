"""Offline corpus tests: source syntax is evidence, never executable input."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
from unittest import mock

from orcexe import cli, intake, slop, task_l0
from tests.unit.test_gitutil import GitFixture
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

    def test_constant_assertions_require_literal_expressions(self):
        cases = {
            'test_literals.py': (
                'def sign(value): return value\n',
                'def test_{}():\n    {}\n',
                ['self.assertEqual(sign("b"), "b!")', 'assert sign(1) == 2',
                 'assert value == 2', 'assert obj.value == 2', 'assert f"{value}" == "b"',
                 'assert [sign(1)] == [2]'],
                ['self.assertEqual(1, 1)', 'assert True', 'assert (1, "b", None) == (1, "b", None)',
                 'assert {"key": [1, True]} == {"key": [1, True]}']),
            'literals.test.ts': (
                'function sign(value) { return value; }\n',
                'test("{}", () => {{ {} }});\n',
                ['assertEquals(sign("b"), "b!");', 'expect(sign(1)).toBe(2);',
                 'assertEquals(value, 2);', 'assertEquals(obj.value, 2);',
                 'assertEquals(await sign(1), 2);', 'expect(1 + value).toBe(2);',
                 'assertEquals(`${value}`, "b");', 'assertEquals([sign(1)], [2]);',
                 'assertEquals({key: sign(1)}, {key: 2});', 'expect(1).toBe(sign(2));',
                 'assertEquals(undefined, null);', 'assertEquals(NaN, Infinity);',
                 'assertEquals(nil, null);'],
                ['assertEquals(1, 1);', 'expect(true).toBe(true);',
                 'assertEquals({key: [1, "b", null]}, {key: [1, "b", null]});',
                 'assertEquals(`plain`, "plain");',
                 'assertEquals([-1, 0xff, 1e3, 1_000, .5], [-1, 255, 1000, 1000, 0.5]);',
                 'expect(1 <= 2).toBe(true);']),
            'Tests/Literals.swift': (
                'func sign(_ value: Int) -> Int { return value }\n',
                'func test{}() {{ {} }}\n',
                ['#expect(sign(1) == 2)', 'XCTAssertEqual(sign(1), 2)',
                 '#expect(value == 2)', '#expect(obj.value == 2)', '#expect(null == nil)',
                 '#expect(await sign(1) == 2)', '#expect(1 + value == 2)',
                 r'XCTAssertEqual("\(value)", "b")',
                 'XCTAssertEqual([sign(1)], [2])'],
                ['#expect(1 == 1)', 'XCTAssertEqual(1, 1)', 'XCTAssertTrue(true)',
                 'XCTAssertEqual([1, 2], [1, 2])', 'XCTAssertEqual(nil, nil)',
                 'XCTAssertEqual((1, "b", nil), (1, "b", nil))',
                 'XCTAssertEqual(["key": [1, 2]], ["key": [1, 2]])',
                 'XCTAssertEqual(1.0, 1.0, accuracy: 0.01)']),
        }
        cases['literals.test.js'] = cases['literals.test.ts']
        files, expected = {}, []
        for path, (header, template, dynamic, constant) in cases.items():
            source = header
            for i, body in enumerate(dynamic + constant):
                if i >= len(dynamic):
                    expected.append((path, source.count('\n') + 1, 'constant_assertions'))
                source += template.format(i, body)
            files[path] = source
        self.commit(files)
        self.assertEqual([(v['path'], v['line'], v['signal']) for v in self.findings('fake_test')],
                         sorted(expected))

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
        self.commit({'a.py': '# noqa\ndef public(): raise NotImplementedError\n',
                     'consumer.py': 'import a\n'})
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

    def test_python_assertion_unit_without_availability_bindings(self):
        path = 'tests/test_aliases.py'
        source = '''import pytest as checks
from pytest import skip as omit
value = product()
other: bool = product()
def helper():
    assert value
def test_helper():
    helper()
def test_skip():
    omit("later")
'''
        helpers = slop._PythonAssertions({path: source})
        self.assertFalse(hasattr(helpers, 'availability'))
        unit = helpers.unit(path)
        tree, parents, imports = unit
        self.assertIs(helpers.unit(path), unit)
        self.assertEqual(imports, {'checks': ('pytest', '', 0), 'omit': ('pytest', 'skip', 0)})
        helper, test_helper, test_skip = tree.body[-3:]
        self.assertIs(parents[test_helper], tree)
        self.assertEqual(helpers.resolve(path, test_helper, 'helper'), ((path, helper), False))
        self.assertEqual(helpers.assertions(path, test_helper), [True])
        self.assertEqual(helpers.callee(path, test_skip.body[0].value.func), 'pytest.skip')
        findings = []
        slop._python(path, source, lambda *finding: findings.append(finding), helpers)
        self.assertEqual(findings, [('fake_test', 9, 'no_assertions'),
                                    ('skipped_test', 10, 'runtime_skip')])
        markers = []
        task_l0._python_markers(source, lambda *finding: markers.append(finding), path, helpers)
        self.assertEqual(markers, [('skipped_test', 10, 'runtime_skip'),
                                  ('skipped_test', 9, 'hollow_test')])

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
                         [('A.swift', 2), ('A.swift', 3)] + [('tests/test_skips.py', n) for n in (3, 6, 10, 12, 14, 25)])
        self.assertTrue(all(v['signal'] == 'conditional_skip' for v in findings if v['advisory']))
        self.assertEqual([v['line'] for v in findings if not v['advisory']],
                         [4, 5, 6, 16, 17, 18, 19, 20, 23, 27, 28, 30])

    def test_runtime_skip_under_non_constant_if_or_except_is_conditional(self):
        self.commit({'tests/test_runtime_guards.py': '''import os, unittest
from orcexe import isolation
class Guards(unittest.TestCase):
    def test_backend(self):
        if isolation.backend() == 'none':
            self.skipTest('no isolation backend')
        assert product()
    def test_verdict(self):
        if self.accepted_verdict != 'accepted':
            self.skipTest('no accepted verdict')
        else:
            assert product()
    def test_symlink(self):
        try:
            os.symlink('a', 'b')
        except OSError:
            self.skipTest('symlinks unavailable')
        assert product()
    def test_constant(self):
        if True:
            self.skipTest('always')
        assert product()
    def test_unconditional(self):
        self.skipTest('later')
        assert product()
    def test_try_body(self):
        try:
            self.skipTest('not guarded by the handler')
        except OSError:
            pass
        assert product()
'''})
        self.assertEqual([(v['line'], v['signal'], v['advisory']) for v in self.findings('skipped_test')],
                         [(6, 'conditional_skip', True), (10, 'conditional_skip', True),
                          (17, 'conditional_skip', True), (21, 'runtime_skip', False),
                          (24, 'runtime_skip', False), (28, 'runtime_skip', False)])

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

    def test_deno_checks_and_hollow_control(self):
        self.commit({'tests/checks_test.ts': '''import { assertEquals, assertThrows, assertRejects } from "jsr:@std/assert";
Deno.test("conditional throw", () => { if (product() !== 2) { throw new Error("wrong"); } });
Deno.test("throw", () => { throw new Error("wrong"); });
Deno.test("std equals", () => { assertEquals(product(), 2); });
Deno.test("std throws", () => { assertThrows(() => product()); });
Deno.test("std rejects", async () => { await assertRejects(() => product()); });
Deno.test("receiver", () => { custom.verifyResult(product()); });
Deno.test("should", () => { custom.shouldMatch(product()); });
Deno.test("fail", () => { t.fail("wrong"); });
Deno.test("hollow", () => { product(); });
'''})
        findings = self.findings('fake_test')
        self.assertEqual([(v['line'], v['signal']) for v in findings], [(10, 'no_assertions')])
        self.assertEqual(self.findings('dependency'), [])

    def test_python_raise_and_assertion_receivers(self):
        self.commit({'tests/test_checks.py': '''def test_conditional_raise():
    if product() != 2:
        raise AssertionError("wrong")
def test_raise():
    raise ValueError("wrong")
def test_receiver():
    context.expectEqual(product(), 2)
def test_should():
    context.shouldMatch(product())
def test_verify():
    context.verifyResult(product())
def test_fail():
    fail("wrong")
def test_namespace():
    assert_module.assertEquals(product(), 2)
def test_hollow():
    product()
'''})
        self.assertEqual([(v['line'], v['signal']) for v in self.findings('fake_test')],
                         [(16, 'no_assertions')])

    def test_scripts_do_not_export_but_imported_modules_do(self):
        script = 'UNUSED = 1\ndef unused(): return 1\n'
        self.commit({'standalone.py': script,
                     'executable.py': '#!/usr/bin/env python3\n' + script,
                     'guarded.py': script + 'if __name__ == "__main__":\n    print("run")\n',
                     'scripts/task.py': script, 'bin/task.py': script, 'tools/task.py': script,
                     'consumer.py': 'import executable, guarded\nfrom library import live\n',
                     'library.py': 'VALUE = 2\ndef live(): return VALUE\ndef unused(): return 1\n',
                     'pkg/__init__.py': '', 'pkg/module.py': script})
        self.assertEqual([(v['path'], v['signal']) for v in self.findings('dead_code')],
                         [('library.py', 'unreferenced_export:unused'),
                          ('pkg/module.py', 'unreferenced_export:UNUSED'),
                          ('pkg/module.py', 'unreferenced_export:unused')])

    def test_same_file_references_are_live(self):
        self.commit({'module.py': '''VALUE = 2
def leaf(): return VALUE
async def async_leaf(): return VALUE
def caller(): return leaf(), async_leaf()
def unused(): return 1
''', 'consumer.py': 'from module import caller\n',
                     'module.ts': '''export const VALUE = 2;
export function leaf() { return VALUE; }
export function caller() { return leaf(); }
export function unused() { return 1; }
''', 'consumer.ts': 'import { caller } from "./module"; caller();\n'})
        self.assertEqual([(v['path'], v['signal']) for v in self.findings('dead_code')],
                         [('module.py', 'unreferenced_export:unused'),
                          ('module.ts', 'unreferenced_export:unused')])

    def test_deno_specifiers_import_maps_and_undeclared_control(self):
        self.commit({'deno.json': json.dumps({'imports': {'@std/': 'jsr:@std/', 'mapped': './local.ts'}}),
                     'import_map.json': json.dumps({'imports': {'other/': 'https://example.invalid/'}}),
                     'main.ts': '''import 'jsr:@std/assert';
import 'npm:@scope/package@1/subpath';
import 'node:fs/promises';
import 'https://example.invalid/mod.ts';
import 'http://example.invalid/mod.ts';
import '@std/assert';
import 'mapped';
import 'other/module';
import 'mapped-but-undeclared';
'''})
        self.assertEqual([(v['line'], v['signal']) for v in self.findings('dependency')],
                         [(9, 'undeclared_import')])

    def test_python_local_modules_sys_path_and_lock_declarations(self):
        self.commit({'scripts/check.py': '''import sys
sys.path.insert(0, "lib")
sys.path.append("helpers")
import inserted, appended, anywhere, locked_uv, locked_poetry, locked_pipfile
import third_party_missing
''', 'lib/inserted.py': '', 'helpers/appended/__init__.py': '',
                     'unrelated/deep/anywhere.py': '',
                     'locks/uv.lock': 'version = 1\n[[package]]\nname = "locked-uv"\nversion = "1"\n',
                     'poetry.lock': '[[package]]\nname = "locked-poetry"\nversion = "1"\n',
                     'Pipfile.lock': '{"develop":{"locked-pipfile":{"version":"==1"}}}',
                     'package-lock.json': '{"packages":{"node_modules/locked-js":{"version":"1"}}}',
                     'main.js': "import 'locked-js/subpath';\nimport 'missing-js';\n"})
        self.assertEqual([(v['path'], v['line'], v['signal']) for v in self.findings('dependency')],
                         [('main.js', 2, 'undeclared_import'), ('scripts/check.py', 5, 'undeclared_import')])

    def test_swift_checks_and_hollow_control(self):
        self.commit({'Tests/Checks.swift': '''func testAssert() { XCTAssertEqual(product(), 2) }
func testFail() { XCTFail("wrong") }
func testExpect() { #expect(product() == 2) }
@Test func validates() { #require(product() == 2) }
func testThrow() throws { if product() != 2 { throw Wrong.value } }
func testHollow() { product() }
'''})
        self.assertEqual([(v['line'], v['signal']) for v in self.findings('fake_test')],
                         [(6, 'no_assertions')])

    def test_external_assertion_libraries_and_npm_runtime_usage(self):
        self.commit({'tests/test_external.py': '''from assertion_library import assertEquals
def test_external():
    assertEquals(product(), 2)
''', 'tests/external.test.ts': '''import { assertEquals } from '@std/assert';
test("mapped", () => { assertEquals(product(), 2); });
''', 'deno.json': '{"imports":{"@std/":"jsr:@std/"}}',
                     'package.json': '{"dependencies":{"lodash":"*","@scope/pkg":"*"}}',
                     'main.ts': "import 'npm:lodash@1/map';\nimport 'npm:@scope/pkg@1/subpath';\n"})
        self.assertEqual(self.findings('fake_test'), [])
        # The deliberately undeclared Python assertion library remains visible.
        self.assertEqual([(v['path'], v['line'], v['signal']) for v in self.findings('dependency')],
                         [('tests/test_external.py', 1, 'undeclared_import')])

    def test_malformed_import_maps_and_locks_are_unseen(self):
        self.commit({'deno.json': '{"imports":[]}',
                     'package-lock.json': '{"dependencies":null}',
                     'Pipfile.lock': '{"default":[]}',
                     'main.ts': "import 'undeclared';\n"})
        report = self.scan()
        self.assertEqual({v['path'] for v in report['unseen'] if 'path' in v},
                         {'deno.json', 'package-lock.json', 'Pipfile.lock'})
        self.assertEqual([v['signal'] for v in report['findings'] if v['class'] == 'dependency'],
                         ['undeclared_import'])

    # T64b: precision families measured on real repositories (synthetic fixtures).

    def test_swift_imported_assertion_helpers_overloads_and_product_boundaries(self):
        self.commit({'Sources/DemoTestSupport/Checks.swift': '''import XCTest
import Demo
public func AssertRender<T>(_ value: T, _ expected: String) {
  XCTAssertEqual(String(describing: value), expected)
}
public func AssertHollow(_ value: Int) {
  _ = value
}
extension XCTest {
  public func AssertRun(command: String, expected: String? = nil) throws -> String {
    try AssertRun(command: command.split(separator: " ").map(String.init), expected: expected)
  }
  public func AssertRun(command: [String], expected: String? = nil) throws -> String {
    #if os(Windows)
    throw XCTSkip("Unsupported on this platform")
    #endif
    let output = command.joined()
    if let expected = expected { XCTAssertEqual(output, expected) }
    return output
  }
  public func verifyManual(page: Int) throws {
    _ = try AssertRun(command: "manual \\(page)")
  }
}
''', 'Sources/DemoTestSupport/Overloads.swift': '''import XCTest
public func AssertMaybe(_ value: Int) { XCTAssertEqual(value, 1) }
public func AssertMaybe(_ value: String) { print(value) }
''', 'Sources/Demo/Product.swift': '''public func AssertProduct(_ value: Int) {
  assert(value > 0)
}
''', 'Tests/DemoTests/DemoTests.swift': '''import DemoTestSupport
import XCTest
@testable import Demo

final class DemoTests: XCTestCase {
  func testRender() {
    AssertRender(1, "1")
  }
  func testRunString() throws {
    try AssertRun(command: "demo 1 2", expected: "3")
  }
  func testManual() throws {
    guard #available(macOS 12, *) else { return }
    try verifyManual(page: 1)
  }
  func testHollowHelper() {
    AssertHollow(product())
  }
  func testProductNamedAssert() {
    AssertProduct(product())
  }
  func testUnresolvedCapitalised() {
    VerifyExternal(product())
  }
  func testAvailabilityOnly() {
    guard #available(macOS 12, *) else { return }
    product()
  }
  func testOverloadUnchecked() {
    AssertMaybe(product())
  }
}
'''})
        self.assertEqual([(v['line'], v['signal']) for v in self.findings('fake_test')
                          if v['path'] == 'Tests/DemoTests/DemoTests.swift'],
                         [(16, 'no_assertions'), (19, 'no_assertions'), (25, 'no_assertions'),
                          (29, 'constant_assertions')])
        # The platform skip inside the helper is still a scored skip.
        self.assertEqual([(v['path'], v['line'], v['advisory']) for v in self.findings('skipped_test')],
                         [('Sources/DemoTestSupport/Checks.swift', 15, False)])

    def test_python_nested_callbacks_product_names_and_mixin_checks(self):
        self.commit({'src/app/report.py': '''def test_count(text):
    return text.count("ok")
def test_commands(commands):
    return [v for v in commands if v]
''', 'tests/test_cli.py': '''import click

def test_invoked_callback(runner):
    @click.command()
    def test(count):
        assert count == 1
    runner.invoke(test, ["1"])

def test_outer_asserts(runner):
    @click.command()
    def test(count):
        click.echo(count)
    result = runner.invoke(test, ["1"])
    assert result.output == "1\\n"

def test_registered_subcommand(runner):
    @click.group()
    def cli():
        pass
    @cli.command()
    def sub():
        assert click.get_current_context() is not None
    runner.invoke(cli, ["sub"])

def test_never_referenced(runner):
    @click.command()
    def cli(name):
        result = runner.invoke(cli, ["-"])
        assert result.exception is None

@click.command()
def test_registered_at_top_level():
    click.echo("not collected by the runner")

class Scenarios:
    def test_mixin(self):
        self.check_scenarios(full=True)
    def test_mixin_hollow(self):
        self.run_scenarios(full=True)
'''})
        self.assertEqual([(v['path'], v['line']) for v in self.findings('fake_test')],
                         [('tests/test_cli.py', 25), ('tests/test_cli.py', 38)])

    def test_python_abstract_methods_and_defensive_test_branches(self):
        self.commit({'src/shell.py': '''class Shell:
    def complete_args(self):
        """Return args. This must be implemented by subclasses."""
        raise NotImplementedError
    def render(self, item):
        """Format an item; subclasses override this."""
        raise NotImplementedError
    def later(self):
        """Render later."""
        raise NotImplementedError
def choose(kind):
    if kind == "a":
        return 1
    else:
        raise NotImplementedError(kind)
''', 'tests/test_flags.py': '''def make(kind):
    if kind is str:
        return "s"
    elif kind is int:
        return 1
    else:
        raise NotImplementedError(kind)
def test_make():
    assert make(str) == "s"
'''})
        self.assertEqual([(v['path'], v['line']) for v in self.findings('stub')],
                         [('src/shell.py', 10), ('src/shell.py', 15)])

    def test_python_type_checking_imports_are_not_dependencies(self):
        self.commit({'pyproject.toml': '[project]\nname = "demo"\ndependencies = []\n',
                     'src/demo/__init__.py': '', 'src/demo/io.py': '''import typing as t
from typing import TYPE_CHECKING
if t.TYPE_CHECKING:
    from _typeshed import ReadableBuffer
if TYPE_CHECKING:
    import typeonlylib
else:
    import runtimefallback
import undeclared_runtime
'''})
        self.assertEqual([(v['line'], v['signal']) for v in self.findings('dependency')],
                         [(8, 'undeclared_import'), (9, 'undeclared_import')])

    def test_swift_format_lint_script_is_a_lint_check(self):
        base = {'Package.swift': '// swift-tools-version:5.9\n', 'Sources/Demo/Demo.swift': 'public let x = 1\n'}
        self.commit(dict(base, **{'.swift-format': '{"version": 1}\n'}))
        self.assertIn('no_lint', {v['signal'] for v in self.findings('missing_checks')})
        self.commit({'Scripts/format.sh': '#!/bin/bash\ngit ls-files -z "*.swift" | xargs -0 swift format lint --strict\n'})
        self.assertNotIn('no_lint', {v['signal'] for v in self.findings('missing_checks')})

    def test_swift_format_lint_requires_a_committed_config(self):
        self.commit({'Package.swift': '// swift-tools-version:5.9\n',
                     'Scripts/format.sh': '#!/bin/bash\nswift format lint --strict Sources\n'})
        self.assertIn('no_lint', {v['signal'] for v in self.findings('missing_checks')})
