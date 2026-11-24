"""Class-bound guard/CAS invariants, discovered across the entire Python surface.

No module inventory: resolve imports and follow guarded adapter parameters into
helpers. Manual `run` and map-manager calls have no task/contract: they are
identified by their RunRequest task/profile, not by their source pathname.
"""

import ast
import copy
import unittest

from tests.security import PrivateHomes, SOURCE, dotted, python_sources


class Surface:
    def __init__(self, sources=None):
        sources = sources if sources is not None else {
            str(p.relative_to(SOURCE)) if SOURCE in p.parents else str(p): p.read_text()
            for p in python_sources()}
        self.units = []
        for path, source in sources.items():
            tree = ast.parse(source, filename=path)
            module = path.replace('/', '.').removesuffix('.py')
            aliases = {}
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    for name in node.names:
                        aliases[name.asname or name.name] = (node.module or '') + '.' + name.name
                elif isinstance(node, ast.Import):
                    for name in node.names:
                        aliases[name.asname or name.name] = name.name
            parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            self.units.append((path, module, tree, aliases, parents))

    @staticmethod
    def name(node, aliases, module):
        value = dotted(node)
        root, dot, tail = value.partition('.')
        return aliases.get(root, module + '.' + root) + (dot + tail if dot else '')

    @staticmethod
    def ancestors(node, parents):
        while node in parents:
            node = parents[node]
            yield node

    def calls(self):
        for path, module, tree, aliases, parents in self.units:
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    yield path, module, tree, aliases, parents, node

    def cas_errors(self):
        errors, found = [], 0
        assignments_by_path = {path: {n.targets[0].id: n.value for n in ast.walk(tree)
                               if isinstance(n, ast.Assign) and len(n.targets) == 1
                               and isinstance(n.targets[0], ast.Name)}
                               for path, module, tree, aliases, parents in self.units}
        for path, module, tree, aliases, parents, call in self.calls():
            # Flatten argv containers and named argv assignments as well as
            # variadic Git helper arguments. Inspect every update-ref site.
            assignments = assignments_by_path[path]

            def flatten(node, seen=()):
                if isinstance(node, ast.Name) and node.id in assignments and node.id not in seen:
                    return flatten(assignments[node.id], seen + (node.id,))
                if isinstance(node, (ast.List, ast.Tuple)):
                    return [v for elt in node.elts for v in flatten(elt, seen)]
                if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
                    return flatten(node.left, seen) + flatten(node.right, seen)
                return [node]

            argv = [v for arg in call.args for v in flatten(arg)]
            if not any(isinstance(n, ast.Constant) and n.value == 'update-ref' for n in argv):
                continue
            targets = [i for i, n in enumerate(argv) if
                       isinstance(n, ast.Constant) and n.value == 'refs/orcexe/ledger-head'
                       or self.name(n, aliases, module) == 'orcexe.ledger.HEAD_REF']
            for index in targets:
                found += 1
                tail = argv[index + 1:]
                if len(tail) != 2 or isinstance(tail[1], ast.Constant) and tail[1].value is None:
                    errors.append((path, call.lineno, 'ledger-head update lacks old oid'))
        return errors, found

    def guard_errors(self):
        errors, direct, wrapped, helpers = [], 0, 0, set()
        for path, module, tree, aliases, parents, call in self.calls():
            if not isinstance(call.func, ast.Attribute) or call.func.attr != 'run':
                continue
            receiver = dotted(call.func.value)
            # Process backends and super().run implement adapters; they are not
            # model dispatches. Any other run receiver must be justified below.
            resolved = self.name(call.func.value, aliases, module)
            if resolved in ('orcexe.proc', 'orcexe.isolation', 'subprocess'):
                continue
            if isinstance(call.func.value, ast.Call) and dotted(call.func.value.func) == 'super':
                continue
            if receiver.endswith(('guard', '_guard')) or (
                    isinstance(call.func.value, ast.Call) and dotted(call.func.value.func).endswith('Guard')):
                continue
            ancestors = list(self.ancestors(call, parents))
            function = next((n for n in ancestors if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))), None)
            guarded = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                          and n.func.attr == 'run' and (dotted(n.func.value).endswith(('guard', '_guard'))
                          or isinstance(n.func.value, ast.Call)
                          and dotted(n.func.value.func).endswith('Guard')) for n in ancestors)
            if guarded:
                direct += 1
                continue
            parameters = {a.arg for a in function.args.args + function.args.kwonlyargs} if function else set()
            if receiver in parameters and receiver == 'adapter':
                helpers.add(module + '.' + function.name)
                continue
            requests = [n for n in ast.walk(function) if isinstance(n, ast.Call)
                        and dotted(n.func).endswith('RunRequest')] if function else []
            def field(req, key, index):
                return next((k.value for k in req.keywords if k.arg == key),
                            req.args[index] if len(req.args) > index else None)
            # Explicitly ungoverned manual run: run_id is both run and task id;
            # map manager: task="map", profile="manager". No task lane dispatch.
            nongoverned = requests and all(
                ast.dump(field(req, 'task', 1)) == ast.dump(field(req, 'run_id', 0))
                and dotted(field(req, 'profile', 5)) == 'args.profile'
                or isinstance(field(req, 'task', 1), ast.Constant)
                and field(req, 'task', 1).value == 'map'
                and isinstance(field(req, 'profile', 5), ast.Constant)
                and field(req, 'profile', 5).value == 'manager' for req in requests)
            if not nongoverned:
                errors.append((path, call.lineno, 'unguarded adapter run'))
        reached = set()
        for path, module, tree, aliases, parents, call in self.calls():
            name = self.name(call.func, aliases, module)
            if name not in helpers:
                continue
            reached.add(name)
            adapter = next((k.value for k in call.keywords if k.arg == 'adapter'), None)
            if not (isinstance(adapter, ast.Call) and dotted(adapter.func).endswith('guard.wrap')
                    and any(k.arg == 'with_main' and isinstance(k.value, ast.Constant)
                            and k.value.value is True for k in adapter.keywords)):
                errors.append((path, call.lineno, 'review helper needs guard.wrap(with_main=True)'))
            else:
                wrapped += 1
        for helper in helpers - reached:
            errors.append((helper, 0, 'raw adapter helper has no guarded callers'))
        return errors, direct, wrapped


class BoundaryScanTests(PrivateHomes):
    def test_every_ledger_head_update_uses_cas(self):
        errors, found = Surface().cas_errors()
        self.assertGreater(found, 0, 'CAS scan must reach the shipped writer')
        self.assertEqual(errors, [])

    def test_every_executor_and_review_dispatch_is_guarded(self):
        errors, direct, wrapped = Surface().guard_errors()
        self.assertGreaterEqual(direct, 3, 'task, do and the guard wrapper must be observed')
        self.assertGreaterEqual(wrapped, 2, 'both L1 and auditor must be observed')
        self.assertEqual(errors, [])

    def test_scans_reject_new_modules_and_argv_forms(self):
        sources = {
            'orcexe/new_entry.py': 'from orcexe.ledger import HEAD_REF as ref\n'
            'def dispatch(adapter, req):\n    adapter.run(req)\n'
            'def change(git):\n    argv = ["git", "update-ref", ref, "new"]\n    git(argv)\n'}
        surface = Surface(sources)
        self.assertTrue(surface.cas_errors()[0])
        self.assertTrue(surface.guard_errors()[0])

    def test_every_context_dispatch_checks_the_approved_allowlist(self):
        found = 0
        for path, module, tree, aliases, parents, call in Surface().calls():
            if not dotted(call.func).endswith('run_task') or not any(
                    k.arg == '_context' for k in call.keywords):
                continue
            found += 1
            loop = next((n for n in Surface.ancestors(call, parents) if isinstance(n, ast.For)), None)
            self.assertIsNotNone(loop, (path, call.lineno, 'context dispatch must skip unapproved tasks'))
            checks = [n for n in ast.walk(loop) if isinstance(n, ast.If) and n.lineno < call.lineno
                      and any(isinstance(c, ast.Continue) for stmt in n.body for c in ast.walk(stmt))]
            self.assertTrue(any(isinstance(n, ast.Compare) and any(isinstance(op, ast.NotIn) for op in n.ops)
                                and any(isinstance(v, ast.Subscript) and isinstance(v.slice, ast.Constant)
                                        and v.slice.value == 'approved' for v in n.comparators)
                                for check in checks for n in ast.walk(check.test)), (path, call.lineno))
        self.assertGreater(found, 0)

    def test_deleting_each_guard_or_review_flag_is_detected(self):
        original = Surface()
        mutations = 0
        for path, module, tree, aliases, parents, call in original.calls():
            replacement = None
            replacements = []
            if dotted(call.func).endswith('guard.wrap'):
                replacement = call.args[0]
                without_flag = copy.deepcopy(call)
                without_flag.keywords = [k for k in without_flag.keywords if k.arg != 'with_main']
                replacements.append(without_flag)
            elif (isinstance(call.func, ast.Attribute) and call.func.attr == 'run'
                  and call.args and isinstance(call.args[0], ast.Lambda)):
                replacement = call.args[0].body
            if replacement is None:
                continue
            replacements.append(replacement)
            for replacement in replacements:
                sources = {p: ast.unparse(t) for p, m, t, a, par in original.units}
                changed = copy.deepcopy(tree)
                class Delete(ast.NodeTransformer):
                    def visit_Call(self, node):
                        if node.lineno == call.lineno and node.col_offset == call.col_offset:
                            return copy.deepcopy(replacement)
                        return self.generic_visit(node)
                sources[path] = ast.unparse(Delete().visit(changed))
                self.assertTrue(Surface(sources).guard_errors()[0], (path, call.lineno))
                mutations += 1
        self.assertGreaterEqual(mutations, 7)
