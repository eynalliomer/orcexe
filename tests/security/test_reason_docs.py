"""Documentation parity derived from executable tables and diagnostic branches.

No second hand-maintained reason list: literal fallbacks, closed exception filters,
route outcomes and gate IDs are collected from the modules that emit them.
"""

import ast
from contextlib import redirect_stderr
import inspect
import io
import re

from tests.security import ROOT, PrivateHomes
from orcexe import do, exitcodes, gate_lite, isolation, mapping, planning, planrun, questions, route, task


def tree(function):
    return ast.parse(inspect.getsource(function))


def strings(node):
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


def raised(*functions):
    values = set()
    for function in functions:
        for node in ast.walk(tree(function)):
            if isinstance(node, ast.Raise) and isinstance(node.exc, ast.Call) and node.exc.args:
                arg = node.exc.args[0]
                if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                    values.add(arg.value)
    return values


def diagnostic_code(reason):
    # Execute the shared emitter so changing its code selection also changes
    # documentation expectations. No child processes or persistent writes.
    with redirect_stderr(io.StringIO()):
        return mapping.report_failure(ValueError(reason), reason, 'context', (reason,))


def failure_reasons(function):
    """report_failure emits either a phase fallback or a closed expected reason."""
    result = set()
    for node in ast.walk(tree(function)):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Tuple) and target.elts and isinstance(target.elts[0], ast.Name) and target.elts[0].id == 'failure':
                    result.add(node.value.elts[0].value)
                elif isinstance(target, ast.Name) and target.id == 'failure' and isinstance(node.value, ast.Constant):
                    result.add(node.value.value)
        if isinstance(node, ast.Call) and (isinstance(node.func, ast.Attribute) and node.func.attr == 'report_failure' or isinstance(node.func, ast.Name) and node.func.id == 'report_failure'):
            if len(node.args) > 3 and isinstance(node.args[3], ast.Tuple):
                result.update(ast.literal_eval(node.args[3]))
    return {reason: diagnostic_code(reason) for reason in result}


def manager_reasons():
    # ManagerUnavailable.__init__ owns the allowed runtime vocabulary.
    values = set()
    for node in ast.walk(tree(mapping.ManagerUnavailable)):
        if isinstance(node, ast.Tuple):
            values.update(ast.literal_eval(node))
        if isinstance(node, ast.IfExp) and isinstance(node.orelse, ast.Constant):
            values.add(node.orelse.value)
    return {'manager_unavailable: ' + reason: diagnostic_code('manager_unavailable: ' + reason) for reason in values}


def exception_fallbacks(function):
    result = set()
    for node in ast.walk(tree(function)):
        if isinstance(node, ast.ExceptHandler):
            for child in ast.walk(node):
                if isinstance(child, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'reason' for t in child.targets):
                    # Filters such as str(error) in ('form_tampered', ...) plus
                    # the fallback arm are the only strings on this assignment.
                    result.update(strings(child.value))
    return result


def code_value(node):
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) and node.value.id == 'exitcodes':
        return getattr(exitcodes, node.attr)
    raise AssertionError('new exit expression needs collector support: ' + ast.dump(node))


def result_strings(node):
    if isinstance(node, ast.IfExp):
        return result_strings(node.body) | result_strings(node.orelse)
    return {node.value} if isinstance(node, ast.Constant) and isinstance(node.value, str) else set()


def terminal_nodes(node):
    # A lock-reclamation callback records a previous run's state, not this CLI
    # invocation's terminal reason. Only nested functions producing an exit code
    # participate in the current command's diagnostic mapping.
    yield node
    for child in ast.iter_child_nodes(node):
        if isinstance(child, ast.FunctionDef) and child.name != 'run_do':
            if not any(isinstance(n, ast.keyword) and n.arg == 'exit_code' for n in ast.walk(child)):
                continue
        yield from terminal_nodes(child)


def do_reasons():
    result = {}
    def add(reason, code):
        result.setdefault(reason, set()).add(code)
    source = tree(do.run_do)
    internal = next(n for n in ast.walk(source) if isinstance(n, ast.FunctionDef) and n.name == 'internal_error')
    internal_code = next(code_value(n.value) for n in ast.walk(internal)
                         if isinstance(n, ast.keyword) and n.arg == 'exit_code')
    for node in ast.walk(source):
        if isinstance(node, ast.Call):
            kwargs = {kw.arg: kw.value for kw in node.keywords}
            if 'reason' in kwargs and isinstance(kwargs['reason'], ast.Constant):
                # summary.update carries its own authoritative code.
                if 'exit_code' in kwargs:
                    add(kwargs['reason'].value, code_value(kwargs['exit_code']))
            if isinstance(node.func, ast.Name) and node.func.id == 'blocked':
                reason, code = node.args[1:3]
                if isinstance(reason, ast.Constant):
                    add(reason.value, code_value(code))
                elif isinstance(reason, ast.BinOp) and isinstance(reason.left, ast.Constant):
                    if reason.left.value == 'lane_':
                        # classify_repo only returns the three supported lanes;
                        # standard/risky share this literal unsupported return.
                        for lane in planning.LANES:
                            if lane != 'fast':
                                add('lane_' + lane, code_value(code))
                    elif reason.left.value == 'route_':
                        for branch in ast.walk(tree(route.route)):
                            if isinstance(branch, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'reason' for t in branch.targets):
                                for value in result_strings(branch.value):
                                    for skips in (([],) if value == 'no_prior' else ([], [{'reason': 'auth_missing'}], [{'reason': 'quota_exhausted'}])):
                                        add('route_' + value, do._route_exit(dict(skipped=skips)))
    # Gate reason is the first failing check's ID; runtime status overrides its
    # code only at run_status. The map itself is read from the actual .get call.
    status_codes = {}
    for node in ast.walk(source):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and isinstance(node.func.value, ast.Dict):
            keys = [ast.literal_eval(k) for k in node.func.value.keys]
            if 'quota' in keys:
                status_codes = dict(zip(keys, [code_value(v) for v in node.func.value.values]))
    assert status_codes
    for identifier in gate_lite._IDS:
        add(identifier, exitcodes.FAIL)
    for code in status_codes.values():
        add(gate_lite._IDS[0], code)
    # Closed ledger task.state reasons also appear in human diagnostics. Derive
    # their vocabulary from literal reason fields, with internal errors at 70.
    for node in terminal_nodes(source):
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == 'reason' and isinstance(value, ast.Constant):
                    reason = value.value
                    add(reason, internal_code if reason == 'internal_error' else
                        exitcodes.OK if reason == 'gate_accepted' else exitcodes.FAIL)
                elif isinstance(key, ast.Constant) and key.value == 'reason' and isinstance(value, ast.IfExp):
                    for reason in result_strings(value):
                        if reason != 'id':
                            add(reason, exitcodes.OK)
    # A report failure is re-raised into the outer internal-error handler.
    for handler in ast.walk(source):
        if isinstance(handler, ast.ExceptHandler) and any(isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(handler)):
            for node in ast.walk(handler):
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and key.value == 'reason' and isinstance(value, ast.Constant):
                            result[value.value] = {internal_code}
    # Same explicit fallback vocabulary is assigned before the gate report.
    for node in ast.walk(source):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'reason' for t in node.targets):
            if isinstance(node.value, ast.IfExp):
                for reason in result_strings(node.value):
                    add(reason, exitcodes.FAIL)
    # signal_<name> is closed by the handlers installed by run_do.
    for node in ast.walk(source):
        if isinstance(node, ast.Tuple) and all(isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id == 'signal' for n in node.elts) and node.elts:
            for value in node.elts:
                add('signal_' + value.attr, exitcodes.FAIL)
    return result


def collected_tables():
    tables = {'map': failure_reasons(mapping._map_command),
              'questions': failure_reasons(questions.questions_command),
              'answer': {reason: exitcodes.USAGE for reason in exception_fallbacks(questions.answer_command)},
              'plan': dict(planning.EXIT_REASONS), 'task': dict(task.EXIT_REASONS),
              'planrun': dict(planrun.EXIT_REASONS), 'do': do_reasons()}
    for node in ast.walk(tree(task.run_task)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'finish':
            if len(node.args) > 1 and isinstance(node.args[1], ast.Constant):
                assert node.args[1].value in tables['task'], 'unregistered task reason'
    tables['map'].update(manager_reasons())
    tables['plan'].update(manager_reasons())
    # Plan runner preconditions reuse task's validators, but exit 2 on refusal.
    preconditions = raised(task._read_plan, task._plan_path, task._route, planrun._main, planrun.ensure_chain)
    preconditions.update(exception_fallbacks(planrun.run_plan))
    preconditions.add(str(isolation.StateTampered()))
    tables['planrun'].update({reason: exitcodes.USAGE for reason in preconditions})
    # Status is read-only: reasons in nested task records retain their original
    # meanings but do not affect the command's zero exit status.
    status_reasons = raised(planrun.status_command, planrun._main, task._plan_path)
    status_reasons.update(exception_fallbacks(planrun.status_command))
    tables['status'] = {reason: exitcodes.USAGE for reason in status_reasons}
    return {name: {(reason, code) for reason, codes in values.items()
                   for code in (codes if isinstance(codes, set) else {codes})}
            for name, values in tables.items()}


class ReasonDocumentationTests(PrivateHomes):
    def test_all_cli_reason_tables_match_code_without_stale_rows(self):
        tables = collected_tables()
        self.assertEqual(set(tables), {'map', 'questions', 'answer', 'plan', 'planrun', 'task', 'status', 'do'})
        for name in ('README.md', 'plugins/orcexe/skills/orcexe/SKILL.md'):
            text = (ROOT / name).read_text()
            for command, expected in tables.items():
                with self.subTest(file=name, command=command):
                    block = re.search(r'<!-- ' + command + r'-exit-reasons -->\n(.*?)<!-- /' + command + r'-exit-reasons -->', text, re.S)
                    if block is None and name.endswith('SKILL.md') and command != 'plan':
                        shared = re.search(r'<!-- shared-exit-reasons -->\n(.*?)<!-- /shared-exit-reasons -->', text, re.S)
                        self.assertIsNotNone(shared)
                        all_rows = re.findall(r'^\| `([^`]+)` \| (\d+) \| ([^|]+) \|$', shared[1], re.M)
                        for _, _, meaning in all_rows:
                            self.assertTrue(set(meaning.split(': ', 1)[0].split(', ')) <= set(tables) - {'plan'})
                        rows = [(reason, code, meaning) for reason, code, meaning in all_rows
                                if command in meaning.split(': ', 1)[0].split(', ')]
                    else:
                        self.assertIsNotNone(block)
                        rows = re.findall(r'^\| `([^`]+)` \| (\d+) \| ([^|]+) \|$', block[1], re.M)
                    self.assertEqual({(reason, int(code)) for reason, code, meaning in rows}, expected)
                    self.assertEqual(len(rows), len(expected), 'duplicate rows')
                    self.assertTrue(all(meaning.strip() for _, _, meaning in rows))
