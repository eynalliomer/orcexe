"""The default suite must pass in a gate checkout, which has no .git.

orcexe gates a task by running the project's checks in a plain file checkout
of the candidate tree. A default-suite test that reads this repository's own
git metadata or history therefore errors there (T47f). Throughput budgets that
scan this repository live in tests/slow/ instead; tests that build their own
temporary git repository are fine.

This test runs no git: it discovers the default suite (ORCEXE_SLOW_TESTS
unset) with child processes forbidden and statically checks each collected
test method, and the same-class helpers it calls, for this repository's root
handed to git (as a subprocess cwd, to a git-backed orcexe module, or joined
with '.git').
"""

import ast
import inspect
import os
from pathlib import Path
import subprocess
import textwrap
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'plugins' / 'orcexe' / 'src' / 'orcexe'
ROOT_NAMES = {'ROOT'}


def _orcexe_imports(tree):
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == 'orcexe':
                names.update(alias.name for alias in node.names)
            elif node.module.startswith('orcexe.'):
                names.add(node.module.split('.')[1])
        elif isinstance(node, ast.Import):
            names.update(alias.name.split('.')[1] for alias in node.names
                         if alias.name.startswith('orcexe.'))
    return names


def git_backed_modules():
    """orcexe modules that run git directly or through an orcexe import."""
    imports, direct = {}, set()
    for path in SOURCE.glob('*.py'):
        text = path.read_text(encoding='utf-8')
        imports[path.stem] = _orcexe_imports(ast.parse(text))
        if "'git'" in text or '"git"' in text or 'gitutil' in text:
            direct.add(path.stem)
    backed, changed = set(direct), True
    while changed:
        changed = False
        for name, used in imports.items():
            if name not in backed and used & backed:
                backed.add(name)
                changed = True
    return backed


def _is_root(node):
    """ROOT itself (not a subpath such as ROOT / 'tests/fixtures')."""
    if isinstance(node, ast.Name):
        return node.id in ROOT_NAMES
    if isinstance(node, ast.Call) and isinstance(node.func, (ast.Name, ast.Attribute)):
        name = node.func.id if isinstance(node.func, ast.Name) else node.func.attr
        return (name in ('str', 'fspath', 'resolve') and
                any(_is_root(arg) for arg in node.args + [getattr(node.func, 'value', None)]
                    if arg is not None))
    return False


def _is_git_path(node):
    return (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div) and
            _is_root(node.left) and isinstance(node.right, ast.Constant) and
            isinstance(node.right.value, str) and
            node.right.value.strip('/').split('/')[0] == '.git')


def reasons(tree, git_modules):
    """Ways the parsed source hands this repository's root to git."""
    found = []
    for node in ast.walk(tree):
        if _is_git_path(node):
            found.append('joins ROOT with .git')
        if not isinstance(node, ast.Call):
            continue
        if any(keyword.arg == 'cwd' and _is_root(keyword.value) for keyword in node.keywords):
            found.append('runs a child in ROOT')
        func = node.func
        if (isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and
                func.value.id in git_modules and
                any(_is_root(arg) for arg in node.args + [k.value for k in node.keywords])):
            found.append('{}.{}(ROOT)'.format(func.value.id, func.attr))
    return found


def _self_calls(tree):
    return {node.func.attr for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name) and node.func.value.id == 'self'}


def violations(cls, function, git_modules, seen=None):
    """Reasons `function` (or a same-class helper it calls) needs this repo's git."""
    seen = set() if seen is None else seen
    if function in seen:
        return []
    seen.add(function)
    try:
        tree = ast.parse(textwrap.dedent(inspect.getsource(function)))
    except (OSError, TypeError, SyntaxError):
        return []
    found = ['{}: {}'.format(function.__qualname__, reason)
             for reason in reasons(tree, git_modules)]
    for name in sorted(_self_calls(tree)):
        helper = getattr(cls, name, None)
        if inspect.isfunction(helper):
            helper = inspect.unwrap(helper)
            if helper.__module__.startswith('tests.'):
                found.extend(violations(cls, helper, git_modules, seen))
    return found


def _tests(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _tests(item)
        else:
            yield item


class DefaultSuiteNoGitTests(unittest.TestCase):
    def test_default_suite_collects_no_test_needing_this_git_work_tree(self):
        git_modules = git_backed_modules()
        self.assertTrue({'gitutil', 'history', 'intake', 'slop'} <= git_modules, git_modules)
        loader = unittest.TestLoader()
        with mock.patch.dict(os.environ), \
                mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('child')) as popen:
            os.environ.pop('ORCEXE_SLOW_TESTS', None)
            suite = loader.discover(str(ROOT), pattern='test*.py', top_level_dir=str(ROOT))
        popen.assert_not_called()
        self.assertEqual(loader.errors, [])
        tests = list(_tests(suite))
        self.assertGreater(len(tests), 500)
        self.assertFalse([t.id() for t in tests if t.id().startswith('tests.slow.')])
        found = []
        for test in tests:
            method = getattr(type(test), test._testMethodName, None)
            if inspect.isfunction(method):
                found.extend(violations(type(test), inspect.unwrap(method), git_modules))
        self.assertEqual(sorted(set(found)), [])

    def test_detector_flags_the_repository_scans_kept_in_slow(self):
        from tests.slow.test_history import SlowHistoryTests
        from tests.slow.test_intake import SlowIntakeTests
        from tests.slow.test_slop import SlowSlopTests
        git_modules = git_backed_modules()
        for cls, name in ((SlowHistoryTests, 'test_repository_child_cpu_and_python39_import'),
                          (SlowHistoryTests, 'test_wall_budget'),
                          (SlowIntakeTests, 'test_this_repository_budget'),
                          (SlowIntakeTests, 'test_repository_scan_wall_clock_budget'),
                          (SlowSlopTests, 'test_python39_import_and_repo_scan_budget'),
                          (SlowSlopTests, 'test_repository_scan_wall_clock_budget')):
            with self.subTest(test=name):
                self.assertTrue(violations(cls, getattr(cls, name), git_modules))

    def test_detector_on_source_snippets(self):
        def check(source):
            return reasons(ast.parse(source), {'history'})
        self.assertEqual(check("history.scan(ROOT)"), ['history.scan(ROOT)'])
        self.assertEqual(check("history.scan(repo=str(ROOT))"), ['history.scan(ROOT)'])
        self.assertEqual(check("subprocess.run(argv, cwd=ROOT)"), ['runs a child in ROOT'])
        self.assertEqual(check("subprocess.run(argv, cwd=str(ROOT))"), ['runs a child in ROOT'])
        self.assertEqual(check("(ROOT / '.git/HEAD').exists()"), ['joins ROOT with .git'])
        for allowed in ("history.scan(ROOT / 'tests/fixtures')", "history.scan(self.main)",
                        "subprocess.run(argv, cwd=self.main)", "(ROOT / 'README.md').read_text()",
                        "tempfile.TemporaryDirectory(dir=str(ROOT))", "other.scan(ROOT)",
                        "(self.main / '.git/index').read_bytes()"):
            with self.subTest(source=allowed):
                self.assertEqual(check(allowed), [])

    def test_detector_follows_same_class_helpers(self):
        class Probe:
            def test_entry(self):
                return self.helper()

            def helper(self):
                # A stand-in module name, so the suite-wide check does not flag this probe.
                return probe_git.scan(ROOT)  # noqa: F821 - parsed, never run

        found = violations(Probe, Probe.test_entry, {'probe_git'})
        self.assertEqual(len(found), 1)
        self.assertIn('helper: probe_git.scan(ROOT)', found[0])
