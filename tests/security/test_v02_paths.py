"""v0.2 process inventory plus real environments at the final spawn boundary."""

import ast
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
import unittest
from unittest import mock

from tests.security import SOURCE, PrivateHomes, capture_children, dotted
from tests.security import test_child_env as child_checks
from tests.security.test_child_env import secret_values
from tests.security.test_forbidden_argv import forbidden_token
from tests.security.test_home_writes import snapshot, changes
from tests.unit import test_mapping as maps, test_planning as plans, test_task as tasks
from tests.unit import test_task_l1 as evaluators, test_planrun as runs
from orcexe import gate_lite, isolation, task_l0
from orcexe.adapters import ClaudeCodeAdapter, CodexAdapter
from orcexe.manifest import GLOBAL_FORBIDDEN_ARGV


# Every layer entry is intentional. Discovery below rejects a new function or
# module even when it happens to reuse an already tested low-level helper.
SPAWN_SITES = {
    'intake': {'_git'}, 'isolation': {'_probe', 'run'}, 'ledger': {'_git'},
    'task': {'run_task'}, 'run': {'run_command'}, 'planning': {'_plan_command'}, 'planrun': {'run_plan'}, 'task_l1': {'evaluate'},
    'risky': {'audit'},
    'task_l0': {'_tests'}, 'gitutil': {'_git'}, 'proc': {'_WindowsJob.stop', 'run'},
    'gate_lite': {'_git_bytes', '_project', '_probe_python'}, 'do': {'_git', '_resolve_identity.config', 'run_do'},
    'worktree': {'_git'}, 'mapping': {'manager_run'},
    'doctor': {'_prerequisites', '_main_worktree'}, 'history': {'_issues'},
    'adapters/codex': {'CodexAdapter.auth_status'}, 'adapters/codex_appserver': {'AppServer.__enter__'},
    'adapters/base': {'Adapter.detect', 'Adapter.run'},
    'adapters/claude_code': {'ClaudeCodeAdapter.auth_status', 'ClaudeCodeAdapter._run'},
    'adapters/cursor': {'CursorAdapter._probe', 'CursorAdapter._run'},
}
# caller module/function, profile; both L0 invocations must be observed separately.
PATHS = {
    'map': ('mapping', 'manager_run', 'manager'),
    'plan': ('planning', '_plan_command', 'manager'),
    'evaluator-claude': ('task_l1', 'evaluate', 'manager'),
    'evaluator-codex': ('task_l1', 'evaluate', 'review'),
    # T60: the risky-lane auditor is a separate read-only call with the evaluator profiles.
    'auditor-claude': ('risky', 'audit', 'manager'),
    'auditor-codex': ('risky', 'audit', 'review'),
    'task': ('task', 'run_task', 'write'),
    'plan --run': ('planrun', 'run_plan', 'write'),
    'gate': ('gate_lite', '_project', None),
    'L0': ('task_l0', '_tests', None),
    'red-first': ('task_l0', '_tests', None),
}


class InventoryTests(PrivateHomes):
    def test_every_subprocess_layer_caller_has_a_row(self):
        found = {}
        for path in (SOURCE / 'orcexe').rglob('*.py'):
            module = path.relative_to(SOURCE / 'orcexe').with_suffix('').as_posix()
            tree = ast.parse(path.read_text())
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    aliases.update((n.asname or n.name, n.name) for n in node.names)
                elif isinstance(node, ast.ImportFrom):
                    aliases.update((n.asname or n.name, (node.module or '') + '.' + n.name) for n in node.names)

            def visit(node, function=''):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    function = (function + '.' if function else '') + node.name
                if isinstance(node, ast.Call):
                    name = dotted(node.func)
                    root, dot, tail = name.partition('.')
                    name = aliases.get(root, root) + (dot + tail if dot else '')
                    if (name.startswith('subprocess.') and name.rsplit('.', 1)[-1] in
                            ('Popen', 'run', 'call', 'check_call', 'check_output', 'getoutput', 'getstatusoutput')
                            or name.endswith(('proc.run', 'isolation.run'))
                            or name == 'adapter.run' or name.endswith(('mapping.manager_run', 'task.run_task', 'task_l1.evaluate'))
                            or isinstance(node.func, ast.Attribute) and node.func.attr == 'run'
                            and isinstance(node.func.value, ast.Subscript)):
                        found.setdefault(module, set()).add(function)
                for child in ast.iter_child_nodes(node):
                    visit(child, function)
            visit(tree)
        self.assertEqual(found, SPAWN_SITES)
        self.assertEqual(set(PATHS), {'map', 'plan', 'evaluator-claude', 'evaluator-codex',
                                      'auditor-claude', 'auditor-codex',
                                      'task', 'plan --run', 'gate', 'L0', 'red-first'})


def home_snapshot(home):
    entries = snapshot(home)
    for path in home.rglob('*'):
        if path.is_dir() and not path.is_symlink():
            entries[path.relative_to(home).as_posix() + '/'] = ('directory', path.stat().st_mode & 0o777)
    return entries


@contextmanager
def audit(case, path):
    secrets = secret_values()
    case.env.update(secrets)
    case.env['CODEX_HOME'] = str(case.home / '.codex')
    config = case.home / '.codex/config.toml'
    original = config.read_bytes() if config.exists() else b''
    before = home_snapshot(case.home)
    with capture_children() as records:
        yield records
    child_checks.ChildEnvironmentTests.assert_no_secrets(case, records, secrets)
    for record in records:
        case.assertFalse([arg for arg in record['argv'] if forbidden_token(arg, GLOBAL_FORBIDDEN_ARGV)], record['argv'])
    module, function, profile = PATHS[path]
    matching = [r for r in records if any(p == 'orcexe/' + module + '.py' and f == function
                                         for p, f, _ in r['frames'])]
    case.assertTrue(matching, 'path never reached: ' + path)
    if profile:
        binaries = {v for k, v in case.env.items() if k.startswith('ORCEXE_BIN_')}
        model = [r for r in matching if r['argv'][0] in binaries and ('-p' in r['argv'] or 'exec' in r['argv'])]
        case.assertTrue(model, 'model spawn never reached: ' + path)
        for record in model:
            case.assertEqual(record['env']['HOME'], str(case.home))
            if profile == 'manager':
                case.assertIn('--restricted', record['argv'])
                case.assertIn('--safe-mode', record['argv'])
            elif profile == 'review':
                case.assertIn('read-only', record['argv'])
    # The CLI state dir is the only orcexe-owned HOME write. Tool-owned Codex
    # trust appends are checked exactly below; Claude manager/evaluator writes none.
    modified = changes(before, home_snapshot(case.home))
    state = Path(case.env['ORCEXE_STATE_DIR']).relative_to(case.home).as_posix() + '/'
    allowed = {'.codex/', '.codex/config.toml'} if profile == 'write' else set()
    # Match v0.1's narrow OS-owned system-Python cache exception, including
    # directory ancestors now that empty directory writes are observed too.
    if any(p.startswith('Library/Caches/com.apple.python/') for p in modified):
        allowed.update(('Library/', 'Library/Caches/'))
    case.assertEqual([p for p in modified if p not in allowed and not p.startswith((state, 'Library/Caches/com.apple.python/'))], [])
    if profile == 'write':
        expected = original + b''.join(
            ('\n[projects.{}]\ntrust_level = \"trusted\"\n'.format(json.dumps(r['argv'][r['argv'].index('-C') + 1]))).encode()
            for r in model if 'exec' in r['argv'])
        case.assertEqual(config.read_bytes(), expected)


class MapSecurity(maps.MappingFixture):
    def test_map_manager_environment_argv_and_home(self):
        with audit(self, 'map'):
            code, output, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['schema'], 'orcexe.project_map.v1')


# Reuse setup/helpers without inheriting PlanCLI's integration test methods.
class PlanSecurity(plans.PlanCLI):

    def test_plan_manager_environment_argv_and_home(self):
        with audit(self, 'plan'):
            code, output, error = self.invoke('--goal', 'Fix 1 typo in README.md')
        self.assertEqual(code, 0, error)
        self.assertEqual(json.loads(output)['status'], 'proposed')


class TaskSecurity(tasks.TaskFixture):
    def test_task_and_all_standard_project_steps(self):
        with audit(self, 'task') as records:
            code, result, error = self.invoke()
        self.assertEqual((code, result['state']), (0, 'gate_passed'), (result, error))
        # Actual caller stacks distinguish candidate gate, baseline and red overlay.
        self.assertTrue(any(any(p == 'orcexe/gate_lite.py' and f == '_project'
                               for p, f, _ in r['frames']) for r in records))
        l0 = [r for r in records if any(p == 'orcexe/task_l0.py' and f == '_tests'
                                      for p, f, _ in r['frames'])]
        self.assertEqual(len(l0), 2, 'baseline and red-first must both spawn')
        homes = [r['env']['HOME'] for r in l0]
        self.assertEqual(len(set(homes)), 2)
        self.assertTrue(all(home != str(self.home) for home in homes))
        self.assertTrue(all(not Path(home).exists() for home in homes))

    def test_plan_run_executor_environment_argv_and_home(self):
        self.saved = self.save([plans.task()])
        self.actions.write_text(json.dumps([{'README.md': 'Corrected\n'}]))
        with audit(self, 'plan --run'):
            # This helper only substitutes the fake adapter and acceptance
            # evidence; the CLI, process, Git and project commands remain real.
            code, result, error = runs.PlanRunTests.invoke_plan(self)
        self.assertEqual((code, result['state'], error), (0, 'completed', ''))

    def test_evaluator_environments_profiles_and_home(self):
        for tool in ('claude-code', 'codex'):
            with self.subTest(tool=tool):
                evaluators.EvaluatorTests.evaluator(self, [evaluators.rubric()], tool=tool)
                selection = dict(tool=tool, model='claude-sonnet-5-5' if tool == 'claude-code' else 'gpt-6.1-sol', effort='high')
                with audit(self, 'evaluator-claude' if tool == 'claude-code' else 'evaluator-codex'), \
                        mock.patch.dict(os.environ, self.env, clear=True):
                    adapter = (ClaudeCodeAdapter if tool == 'claude-code' else CodexAdapter)(env=self.env)
                    value = evaluators.task_l1.evaluate(repo_path=self.main, tree=self.head,
                        contract=dict(head=self.head, task_id='T1', done=['Observed']), gate={}, l0={},
                        selection=selection, adapter=adapter, run_dir=self.directory / tool, run_id='security')
                self.assertEqual(value['reason'], None, value)
                self.assertTrue(value['rubric']['overall_pass'])


    def test_auditor_environments_profiles_and_home(self):
        from orcexe import risky
        verdict = dict(schema='orcexe.audit.v1', verdict='pass', findings=[])
        for tool in ('claude-code', 'codex'):
            with self.subTest(tool=tool):
                evaluators.EvaluatorTests.evaluator(self, [verdict], tool=tool)
                selection = dict(tool=tool, model='claude-opus-5-5' if tool == 'claude-code' else 'gpt-6.1-sol',
                                 effort='high')
                with audit(self, 'auditor-claude' if tool == 'claude-code' else 'auditor-codex'), \
                        mock.patch.dict(os.environ, self.env, clear=True):
                    adapter = (ClaudeCodeAdapter if tool == 'claude-code' else CodexAdapter)(env=self.env)
                    value = risky.audit(repo_path=self.main, tree=self.head,
                        contract=dict(head=self.head, task_id='T1', done=['Observed']), notes='', gate={}, l0={},
                        l1={}, selection=selection, adapter=adapter, run_dir=self.directory / ('audit-' + tool),
                        run_id='security')
                self.assertEqual((value['outcome'], value['reason']), ('pass', None), value)


class ProjectPolicyTests(PrivateHomes):
    def test_each_project_path_denies_home_escape_and_loopback_network(self):
        if isolation.backend() == 'none':
            self.skipTest('OS sandbox unavailable; policy construction remains tested below')
        for path in ('gate', 'L0', 'red-first'):
            with self.subTest(path=path), tempfile.TemporaryDirectory() as raw, \
                    socket.socket() as listener:
                root = Path(raw).resolve()
                checkout = root / 'checkout'
                checkout.mkdir()
                (checkout / 'escape').symlink_to(root, target_is_directory=True)
                listener.bind(('127.0.0.1', 0))
                listener.listen(1)
                victim = root / 'outside'
                script = ('import os,socket\nfrom pathlib import Path\n'
                    'Path(os.environ["HOME"],"allowed").write_text("yes")\n'
                    'for target in ' + repr([str(victim), 'escape/outside']) + ':\n'
                    '    try: Path(target).write_text("no")\n'
                    '    except OSError: pass\n'
                    '    else: raise AssertionError("HOME escape")\n'
                    's=socket.socket(); s.settimeout(1)\n'
                    'assert s.connect_ex(' + repr(listener.getsockname()) + ') != 0, "network escaped"\n'
                    'print("Ran 1 test in 0.01s\\n\\nOK")\n')
                command = gate_lite.Command('unittest', (sys.executable, '-B', '-c', script), 'fixture')
                deadline = time.monotonic() + 10
                with capture_children() as records:
                    if path == 'gate':
                        status, detail = gate_lite._project([command], checkout, root / 'logs', 5, deadline)
                        self.assertEqual(status, 'pass', detail)
                    else:
                        result = task_l0._tests([command], checkout, root / 'logs', 5, deadline, root)
                        self.assertEqual(result[0]['exit_code'], 0)
                self.assertFalse(victim.exists())
                self.assertTrue(all(not Path(r['env']['HOME']).exists() for r in records))

    def test_both_os_policies_keep_network_off_and_home_temporary(self):
        for mode in ('seatbelt', 'bwrap'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as raw, \
                    mock.patch.object(isolation, 'backend', return_value=mode), \
                    mock.patch.object(isolation.shutil, 'which', return_value='/fake/bwrap'), \
                    mock.patch.object(isolation.proc, 'run') as run:
                isolation.run(['project-test'], cwd=raw, env={'HOME': os.environ['HOME']})
                argv = run.call_args.args[0]
                env = run.call_args.kwargs['env']
                self.assertNotEqual(env['HOME'], os.environ['HOME'])
                self.assertFalse(Path(env['HOME']).exists())
                if mode == 'seatbelt':
                    self.assertIn('(deny network-outbound)', argv[2])
                    self.assertIn('(deny file-write*)', argv[2])
                else:
                    self.assertIn('--unshare-all', argv)
                    self.assertEqual(argv.count('--bind'), 2)


def load_tests(loader, standard_tests, pattern):
    suite = unittest.TestSuite()
    for cls in (InventoryTests, MapSecurity, PlanSecurity, TaskSecurity, ProjectPolicyTests):
        names = [name for name in cls.__dict__ if name.startswith("test_")]
        suite.addTests(cls(name) for name in names)
    return suite
