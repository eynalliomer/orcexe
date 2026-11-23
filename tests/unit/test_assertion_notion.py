"""Synthetic gate regressions; fixture source is never executed."""

import os
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from tests.unit import test_slop
from tests.unit.test_gitutil import GitFixture
from orcexe import gate_lite, task_l0


def fixtures():
    files = {
        'tests/deno.test.ts': '''import { assertEquals, expectEqual, assertEquals as equal } from "jsr:@std/assert@1";
import * as checks from "jsr:@std/assert@1";
import { requireValue } from "./support";
function leaf(value) { assertEquals(value, "b!"); }
function middle(value) { leaf(value); }
function outer(value) { middle(value); }
function tooFar(value) { outer(value); }
function cycle() { cycle(); }
Deno.test("mixed", () => { assertEquals(sign("b"), "b!"); });
Deno.test("hollow mixed", () => { Rules.check(2); });
Deno.test("imported expect", () => { expectEqual(sign("b"), "b!"); });
Deno.test("hollow expect", () => { Rules.check(2); });
Deno.test("alias", () => { equal(sign("b"), "b!"); });
Deno.test("namespace", () => { checks.assertEquals(sign("b"), "b!"); });
Deno.test("local helper", () => { outer(sign("b")); });
Deno.test("hollow depth", () => { tooFar(sign("b")); });
Deno.test("support helper", () => { requireValue(sign("b")); });
Deno.test("hollow support", () => { Rules.check(2); });
Deno.test("hollow cycle", () => { cycle(); });
''',
        'tests/support.ts': 'export function requireValue(value) { if (!value) { throw new Error("wrong"); } }\n',
        'tests/test_checks.py': '''from tests.support import require_value
from assertion_library import assertEquals as equal
def leaf(value):
    if not value:
        raise AssertionError("wrong")
def middle(value):
    leaf(value)
def outer(value):
    middle(value)
def too_far(value):
    outer(value)
def test_mixed():
    equal(sign("b"), "b!")
def test_hollow_mixed():
    Rules.check(2)
def test_local():
    outer(sign("b"))
def test_hollow_depth():
    too_far(sign("b"))
def test_support():
    require_value(sign("b"))
def test_hollow_support():
    Rules.check(2)
''',
        'tests/support.py': 'def require_value(value):\n    if not value:\n        raise AssertionError("wrong")\n',
    }
    for framework in ('Testing', 'XCTest'):
        # Swift Testing functions deliberately do not use XCTest's test prefix.
        declaration = '@Test func' if framework == 'Testing' else 'func test'
        def test(name, body):
            return '{}{}() throws {{ {} }}\n'.format(
                declaration + (' ' if framework == 'Testing' else ''), name, body)
        source = '''private func require(_ e: Element, timeout t: Double, message m: String) {
    if !e.waitForExistence(timeout: t) { XCTFail(m) }
}
func middle(_ e: Element) { require(e, timeout: 1, message: "wrong") }
func outer(_ e: Element) { middle(e) }
func tooFar(_ e: Element) { outer(e) }
func cycle() { cycle() }
'''
        for name, body in (
                ('Guard', 'guard case .tooFar = f() else { Issue.record("wrong"); return }'),
                ('Fail', 'XCTFail("wrong")'), ('Expect', '#expect(f() == 2)'),
                ('Require', 'try #require(f() == 2)'), ('Unwrap', '_ = try XCTUnwrap(f())'),
                ('Local', 'outer(element())'), ('Support', 'requireSupport(element())'),
                ('Nested', 'func helper() { Issue.record("wrong") }; helper()')):
            source += test(name, body) + test('Hollow' + name, '_ = Rules.check(2)')
        source += test('HollowDepth', 'tooFar(element())')
        source += test('HollowCycle', 'cycle()')
        source += test('HollowNested', 'func helper() { Issue.record("wrong") }; _ = Rules.check(2)')
        source += test('HollowLiteral', 'let s = "Issue.record()"; /* XCTFail() */ _ = Rules.check(2)')
        files['tests/' + framework + '/Checks.swift'] = source
        files['tests/' + framework + '/Support.swift'] = (
            'func requireSupport(_ e: Element) { if !e.exists { Issue.record("wrong") } }\n')
    return files


def hollow_lines(path, source):
    return [n for n, line in enumerate(source.splitlines(), 1)
            if 'hollow' in line.lower() and ('def test_' in line or 'Deno.test(' in line or 'func ' in line)]


class AssertionNotionTests(GitFixture):
    write = test_slop.SlopTests.write
    commit = test_slop.SlopTests.commit
    scan = test_slop.SlopTests.scan

    def test_gate_regressions_and_hollow_twins(self):
        files = fixtures()
        self.commit(files)
        report = self.scan()
        self.assertFalse([v for v in report['unseen'] if v['reason'] == 'malformed_or_unsupported'])
        self.assertEqual([(f['path'], f['line']) for f in report['findings'] if f['class'] == 'fake_test'],
                         sorted((p, n) for p, text in files.items() for n in hollow_lines(p, text)))

    def test_l0_uses_the_same_local_and_support_checks(self):
        files = fixtures()
        for path, text in files.items():
            if not hollow_lines(path, text):
                continue
            with self.subTest(path=path):
                observed = dict(versions=[(dict(path=path), ('old', 'new'))])
                blobs = dict(old=b'', new=text.encode())
                expected = hollow_lines(path, text)
                missing = task_l0._assertion_missing(observed, blobs, files)
                skipped = task_l0._skip_added(observed, blobs, files)
                self.assertEqual([f['line'] for f in missing], expected)
                self.assertEqual([f['line'] for f in skipped], expected)

    def test_imported_assertion_arguments_and_aliases(self):
        source = '''import { assertEquals, expect, expectEqual as equal } from "jsr:@std/assert@1";
Deno.test("mixed", () => { assertEquals(sign("b"), "b!"); });
Deno.test("reverse", () => { assertEquals("b!", sign("b")); });
Deno.test("expect", () => { expect(sign("b")); });
Deno.test("alias", () => { equal(sign("b"), "b!"); });
Deno.test("constant", () => { assertEquals(1, 1); });
Deno.test("constant alias", () => { equal("b!", "b!"); });
Deno.test("hollow", () => { Rules.check(2); });
'''
        self.commit({'tests/arguments.test.ts': source})
        self.assertEqual([(f['line'], f['signal']) for f in self.scan()['findings'] if f['class'] == 'fake_test'],
                         [(6, 'constant_assertions'), (7, 'constant_assertions'), (8, 'no_assertions')])
        default = source.replace(
            'import { assertEquals, expect, expectEqual as equal } from "jsr:@std/assert@1";',
            'import assertEquals from "npm:assert-equals";\n'
            'import expect from "npm:expect-value";\n'
            'import { expectEqual as equal } from "npm:expect-equal";')
        self.commit({'tests/arguments.test.ts': default})
        self.assertEqual([(f['line'], f['signal']) for f in self.scan()['findings'] if f['class'] == 'fake_test'],
                         [(8, 'constant_assertions'), (9, 'constant_assertions'), (10, 'no_assertions')])

    def test_swift_method_helpers_and_scope_boundaries(self):
        source = '''class Checks: XCTestCase {
    private func require(_ e: Element) { if !e.exists { XCTFail("wrong") } }
    func testLocal() { self.require(element()) }
    func testSupport() { requireInherited(element()) }
    func testHollowProduct() { _ = Rules.check(2) }
    func testHollowOther() { otherHelper(element()) }
}
class Other {
    func otherHelper(_ e: Element) { Issue.record("wrong") }
}
func testHollowPrivate() { privateSupport() }
func testHollowOtherSuite() { foreignSupport() }
'''
        files = {'tests/MethodTests/Checks.swift': source,
                 'tests/MethodTests/Support.swift': '''extension XCTestCase {
    func requireInherited(_ e: Element) { if !e.exists { Issue.record("wrong") } }
}
private func privateSupport() { Issue.record("wrong") }
''', 'tests/ForeignTests/Support.swift': 'func foreignSupport() { Issue.record("wrong") }\n'}
        self.commit(files)
        self.assertEqual([(f['path'], f['line']) for f in self.scan()['findings'] if f['class'] == 'fake_test'],
                         [('tests/MethodTests/Checks.swift', n) for n in (5, 6, 11, 12)])
        observed = dict(versions=[(dict(path='tests/MethodTests/Checks.swift'), ('old', 'new'))])
        blobs = dict(old=b'', new=source.encode())
        self.assertEqual([f['line'] for f in task_l0._assertion_missing(observed, blobs, files)], [5, 6, 11, 12])

    def test_l0_compares_helpers_in_their_own_source_version(self):
        source = 'from tests.support import helper\ndef test_value():\n    helper(product())\n'
        observed = dict(versions=[(dict(path='tests/test_value.py'), ('old_test', 'new_test')),
                                  (dict(path='tests/support.py'), ('old_helper', 'new_helper'))])
        blobs = dict(old_test=source.encode(), new_test=(source + '# changed\n').encode(),
                     old_helper=b'def helper(value):\n    assert value\n',
                     new_helper=b'def helper(value):\n    product(value)\n')
        self.assertEqual(task_l0._skip_added(observed, blobs),
                         [dict(path='tests/test_value.py', line=2, signal='hollow_test')])

    def test_l0_reads_unchanged_support_from_frozen_copies(self):
        after = {
            'tests/test_snapshot.py': 'from tests.support import require_value\ndef test_value():\n    require_value(product())\n',
            'tests/support.py': 'def require_value(value):\n    assert value\n',
            'tests/snapshot.test.ts': 'import { requireValue } from "./support";\ntest("value", () => { requireValue(product()); });\n',
            'tests/support.ts': 'export function requireValue(value) { expect(value).toBe(1); }\n',
            'tests/SwiftTests/Snapshot.swift': 'func testValue() { requireValue(product()) }\n',
            'tests/SwiftTests/Support.swift': 'func requireValue(_ v: Int) { #expect(v == 1) }\n',
        }
        before = {p: text.replace('product()', 'oldProduct()') for p, text in after.items()}
        files, versions, blobs, fingerprints = [], [], {}, []
        for i, path in enumerate(p for p in after if 'Snapshot' in p or 'snapshot' in p):
            file = dict(path=path, old_path=path, new_mode='100644', old_mode='100644')
            old, new = 'old' + str(i), 'new' + str(i)
            files.append(file)
            versions.append((file, (old, new)))
            blobs.update({old: before[path].encode(), new: after[path].encode()})
            fingerprints.append((':100644 100644 {} {} M'.format(old, new), path, path))
        observed = dict(tree='candidate', versions=versions, files=files)
        run_dir = self.directory / 'run'
        (run_dir / 'gate').mkdir(parents=True)
        # T55: L0 reads the gate's in-process log copy, as the gate writes it.
        gate_lite._write_log(run_dir / 'gate', 'unittest', 'Ran 2 tests in 0.01s\nOK\n')

        def checkout(repo, tree, scratch, *args):
            copy = Path(scratch) / 'checkout'
            copy.mkdir()
            for path, text in (after if tree == 'candidate' else before).items():
                target = copy / path
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(text)
            return copy

        def runner(commands, checkout, directory, *args):
            # The support snapshot must precede any repository test execution.
            for path in after:
                if 'support' in path.lower():
                    (checkout / path).write_text('')
            return [dict(count=2, skip_counts=dict(skipped=0, xfailed=0, expected_failures=0),
                         exit_code=1 if directory.name == 'red' else 0)]

        with mock.patch.dict(os.environ, ORCEXE_STATE_DIR=str(self.directory / 'state')), \
                mock.patch.object(gate_lite, '_checkout', side_effect=checkout), \
                mock.patch.object(gate_lite, '_blobs', return_value=blobs), \
                mock.patch.object(gate_lite, '_collect', return_value=([], files, fingerprints)), \
                mock.patch.object(task_l0, '_tests', side_effect=runner):
            result = task_l0.run_l0(repo_path=self.main,
                worktree=SimpleNamespace(path=str(self.main), base_commit='base'), observed=observed,
                commands=[gate_lite.Command('unittest', ['python3', '-m', 'unittest'])], gate={}, run_dir=run_dir)
        self.assertEqual({c['id']: c['status'] for c in result['checks']},
                         {identifier: 'pass' for identifier in task_l0.CHECK_IDS})
