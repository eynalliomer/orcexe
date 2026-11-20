"""Bounded, model-free observations of committed source, never a quality gate.

Signals are deliberately syntactic. Dead exports use a cheap identifier search
across other source files and are always advisory. No project code is imported.
"""

import ast
from collections import Counter
import configparser
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import time
import tokenize
from types import FunctionType

from orcexe import fsio, gitutil, intake
from orcexe.gate_lite import _python_code, _tokens

CLASSES = ('fake_test', 'skipped_test', 'suppression', 'stub', 'dead_code',
           'dependency', 'missing_checks')
LANGUAGES = {'.py': 'python', '.js': 'javascript', '.jsx': 'javascript',
             '.mjs': 'javascript', '.cjs': 'javascript', '.ts': 'typescript',
             '.tsx': 'typescript', '.swift': 'swift'}
SUPPRESSION = re.compile(
    r'(?<!\w)(?:type:\s*ignore|pyright:\s*ignore|mypy:\s*ignore|noqa\b|'
    r'pylint:\s*(?:disable|skip-file)|(?:eslint|tslint)-(?:disable|enable)|'
    r'@ts-(?:ignore|nocheck|expect-error)|istanbul\s+ignore|(?:c8|v8)\s+ignore|'
    r'pragma:\s*no\s*(?:cover|branch)|swiftlint:\s*disable)', re.I)
TODO = re.compile(r'\b(?:TODO|FIXME|XXX)\b.*\b(?:implement\w*|stub)\b', re.I | re.S)
# Distribution roots that do not match their import roots. Normalize both sides.
DISTRIBUTIONS = {'yaml': 'pyyaml', 'attr': 'attrs', 'attrs': 'attrs',
                 'dateutil': 'python-dateutil', 'pil': 'pillow',
                 'sklearn': 'scikit-learn', 'bs4': 'beautifulsoup4',
                 'cv2': 'opencv-python', 'dotenv': 'python-dotenv',
                 'crypto': 'pycryptodome', 'jwt': 'pyjwt', 'openssl': 'pyopenssl'}
# A stable public-module union is needed on Python 3.9 (which lacks the sys set).
PY_STDLIB = frozenset('''__future__ __main__ _thread abc aifc argparse array ast
asynchat asyncio asyncore atexit audioop base64 bdb binascii binhex bisect builtins
bz2 calendar cgi cgitb chunk cmath cmd code codecs codeop collections colorsys
compileall concurrent configparser contextlib contextvars copy copyreg cProfile
crypt csv ctypes curses dataclasses datetime dbm decimal difflib dis distutils
doctest email encodings ensurepip enum errno faulthandler fcntl filecmp fileinput
fnmatch fractions ftplib functools gc genericpath getopt getpass gettext glob
graphlib grp gzip hashlib heapq hmac html http idlelib imaplib imghdr imp importlib
inspect io ipaddress itertools json keyword lib2to3 linecache locale logging lzma
mailbox mailcap marshal math mimetypes mmap modulefinder msilib msvcrt
multiprocessing netrc nis nntplib nt ntpath nturl2path numbers operator optparse os
ossaudiodev pathlib pdb pickle pickletools pipes pkgutil platform plistlib poplib
posix posixpath pprint profile pstats pty pwd py_compile pyclbr pydoc pydoc_data
pyexpat queue quopri random re readline reprlib resource rlcompleter runpy sched
secrets select selectors shelve shlex shutil signal site smtpd smtplib sndhdr
socket socketserver spwd sqlite3 sre_compile sre_constants sre_parse ssl stat
statistics string stringprep struct subprocess sunau symbol symtable sys
sysconfig syslog tabnanny tarfile telnetlib tempfile termios textwrap threading
time timeit tkinter token tokenize tomllib trace traceback tracemalloc tty turtle
turtledemo types typing unicodedata unittest urllib uu uuid venv warnings wave
weakref webbrowser winreg winsound wsgiref xdrlib xml xmlrpc zipapp zipfile
zipimport zlib zoneinfo'''.split())
NODE_BUILTINS = frozenset('''assert async_hooks buffer child_process cluster console
constants crypto dgram diagnostics_channel dns domain events fs http http2 https
inspector module net os path perf_hooks process punycode querystring readline repl
sea sqlite stream string_decoder sys test timers tls trace_events tty url util v8
vm wasi worker_threads zlib'''.split())


def _language(path):
    return LANGUAGES.get(PurePosixPath(path).suffix.lower())


def _wanted(path):
    if intake._own_state(path) or intake._excluded(path) or intake._instruction(path):
        return None
    name = PurePosixPath(path).name
    return bool(_language(path) or name in ('package.json', 'pyproject.toml', 'setup.cfg')
                or re.fullmatch(r'requirements[^/]*', name))


def _fetch(repo, entries, unseen, deadline):
    # Intake is a read-only input. Bind its exact reader to a private allow-list
    # namespace rather than mutating its globals (including during concurrent use).
    # Its secret-OID guard, mode checks, caps and batch validation stay intact.
    namespace = dict(intake._fetch.__globals__, _file_kind=_wanted)
    reader = FunctionType(intake._fetch.__code__, namespace, 'slop_blob_reader')
    return reader(repo, entries, unseen, deadline)


def _masked(text):
    return ''.join('\n' if c == '\n' else ' ' for c in text)


def _lex(text, suffix):
    """Return offset-preserving code, comments and tokens; literals stay opaque.

    Templates (including interpolations) are opaque so nested source-like text
    cannot create signals. Regex literals are similarly opaque in JS/TS.
    """
    output, comments, tokens = [], [], []
    i, n = 0, len(text)
    previous = ''
    while i < n:
        start, c = i, text[i]
        if c.isspace():
            output.append(c)
            i += 1
            continue
        if text.startswith('//', i) or text.startswith('/*', i):
            block = text.startswith('/*', i)
            end = text.find('*/' if block else '\n', i + 2)
            if end < 0 and block:
                raise ValueError('unterminated_comment')
            i = n if end < 0 else end + (2 if block else 0)
            raw = text[start:i]
            comments.append((start, raw))
            output.append(_masked(raw))
            continue
        triple = suffix == '.swift' and text.startswith('"""', i)
        if c in "'\"`":
            quote = '"""' if triple else c
            i += len(quote)
            # Template substitutions can themselves contain templates/quotes.
            # Track braces and nested quoted values until the outer backtick.
            stack = []
            while i < n:
                if text[i] == '\\':
                    i += 2
                elif c == '`' and text.startswith('${', i):
                    stack.append('}')
                    i += 2
                elif stack and text[i] in "'\"`":
                    inner = text[i]
                    i += 1
                    while i < n:
                        if text[i] == '\\':
                            i += 2
                        elif text[i] == inner:
                            i += 1
                            break
                        else:
                            i += 1
                elif stack and text[i] == '{':
                    stack.append('}')
                    i += 1
                elif stack and text[i] == '}':
                    stack.pop()
                    i += 1
                elif not stack and text.startswith(quote, i):
                    i += len(quote)
                    break
                else:
                    i += 1
            else:
                raise ValueError('unterminated_literal')
            raw = text[start:i]
            tokens.append(('literal', raw, start, i))
            output.append(_masked(raw))
            previous = 'literal'
            continue
        if c == '/' and suffix != '.swift' and previous in ('', '=', '(', '[', '{', ',', ':',
                                                           'return', '=>', '!', '&&', '||'):
            i += 1
            bracket = False
            while i < n:
                if text[i] == '\\':
                    i += 2
                    continue
                if text[i] == '[':
                    bracket = True
                elif text[i] == ']':
                    bracket = False
                elif text[i] == '/' and not bracket:
                    i += 1
                    while i < n and text[i].isalpha():
                        i += 1
                    break
                elif text[i] == '\n':
                    raise ValueError('unterminated_regex')
                i += 1
            raw = text[start:i]
            output.append(_masked(raw))
            tokens.append(('literal', raw, start, i))
            previous = 'literal'
            continue
        match = re.match(r'[A-Za-z_$][\w$]*|\d+(?:\.\d+)?|===|!==|=>|==|!=|&&|\|\||[^\s]', text[i:])
        raw = match[0]
        i += len(raw)
        output.append(raw)
        tokens.append(('code', raw, start, i))
        previous = raw
    # Reuse the gate lexer on the unambiguous subset; the lexer above extends it
    # for template and regex literals, which gate_lite intentionally rejects.
    _tokens(''.join(output).replace('/', ' '), suffix)
    return ''.join(output), comments, tokens


def _name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return _name(node.value) + '.' + node.attr
    if isinstance(node, ast.Call):
        return _name(node.func)
    if isinstance(node, ast.Subscript):
        return _name(node.value)
    return ''


def _constant(node):
    if isinstance(node, (ast.Constant, ast.Num, ast.Str, ast.NameConstant)):
        return True
    if isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        return all(_constant(v) for v in node.elts)
    if isinstance(node, ast.Dict):
        return all(k is not None and _constant(k) and _constant(v)
                   for k, v in zip(node.keys, node.values))
    if isinstance(node, (ast.UnaryOp, ast.BinOp, ast.BoolOp, ast.Compare)):
        return all(_constant(v) for v in ast.iter_child_nodes(node)
                   if isinstance(v, ast.expr))
    return False


def _body_nodes(function):
    """Do not borrow assertions or stubs from nested function/class declarations."""
    pending = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


def _test_support(path):
    """Only admitted test/support source may supply imported assertions."""
    parts = PurePosixPath(path).with_suffix('').parts
    return any(re.search(r'^(?:tests?|testing|conftest|_?(?:test_)?(?:support|helpers?|fixtures?))$'
                         r'|(?:^test_|[._-](?:test|spec)$)', part, re.I) for part in parts)


class _PythonAssertions:
    """A bounded static call graph; never imports or reads project files."""

    def __init__(self, texts):
        self.texts = texts
        self.units = {}
        self.modules = {}

    def unit(self, path, tree=None):
        if path not in self.units:
            try:
                tree = tree if tree is not None else ast.parse(self.texts[path])
                parents = {child: parent for parent in ast.walk(tree)
                           for child in ast.iter_child_nodes(parent)}
                imports, bindings = {}, {}
                for node in ast.walk(tree):
                    if isinstance(node, ast.Import):
                        for alias in node.names:
                            imports[alias.asname or alias.name.split('.')[0]] = (
                                alias.name if alias.asname else alias.name.split('.')[0], '', 0)
                    elif isinstance(node, ast.ImportFrom):
                        for alias in node.names:
                            imports[alias.asname or alias.name] = (node.module or '', alias.name, node.level)
                    elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                        for target in targets:
                            bindings.setdefault(_name(target), []).append(node.value)
                self.units[path] = (tree, parents, imports, bindings)
            except (SyntaxError, ValueError, RecursionError):
                self.units[path] = None
        return self.units[path]

    def module(self, path, module, level):
        key = (path, module, level)
        if key not in self.modules:
            self.modules[key] = self._module(path, module, level)
        return self.modules[key]

    def _module(self, path, module, level):
        base = PurePosixPath(path).parent
        if level:
            for _ in range(level - 1):
                base = base.parent
            stem = str(base.joinpath(*module.split('.'))) if module else str(base)
            candidates = [stem + '.py', stem + '/__init__.py']
        else:
            stem = module.replace('.', '/')
            candidates = []
            for parent in (base,) + tuple(base.parents):
                candidates.extend((str(parent / (stem + '.py')), str(parent / stem / '__init__.py')))
        for candidate in candidates:
            if candidate in self.texts:
                return candidate
        matches = [p for p in self.texts if p.endswith(('/' + stem + '.py', '/' + stem + '/__init__.py'))]
        return matches[0] if len(matches) == 1 else None

    def method(self, path, cls, name, seen=()):
        key = (path, cls.name)
        if key in seen:
            return None
        unit = self.unit(path)
        for node in cls.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return path, node
        for base in cls.bases:
            base_name = _name(base)
            base_path, base_unit = path, unit
            imported = unit[2].get(base_name)
            if imported:
                module, symbol, level = imported
                base_path = self.module(path, module, level)
                if not base_path or not _test_support(base_path):
                    continue
                base_unit, base_name = self.unit(base_path), symbol
            if base_unit:
                for node in base_unit[0].body:
                    if isinstance(node, ast.ClassDef) and node.name == base_name:
                        result = self.method(base_path, node, name, seen + (key,))
                        if result:
                            return result
        return None

    def resolve(self, path, function, name):
        """Return ((path, declaration), fallback allowed). Product imports veto fallback."""
        unit = self.unit(path)
        if unit is None:
            return None, True
        tree, parents, imports, _ = unit
        pieces = name.split('.')
        scope = function
        if len(pieces) == 1:
            while scope is not None:
                for node in ([] if isinstance(scope, ast.ClassDef) else getattr(scope, 'body', [])):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                        return (path, node), False
                scope = parents.get(scope)
        else:
            owner = next((v for v in tree.body if isinstance(v, ast.ClassDef) and v.name == pieces[0]), None)
            if pieces[0] in ('self', 'cls'):
                owner = parents.get(function)
                while owner is not None and not isinstance(owner, ast.ClassDef):
                    owner = parents.get(owner)
            if owner is not None and len(pieces) == 2:
                target = self.method(path, owner, pieces[1])
                if target:
                    return target, False
        if pieces[0] in imports:
            module, symbol, level = imports[pieces[0]]
            members = ([symbol] if symbol else []) + pieces[1:]
            # Namespace imports and ``from . import support`` may name a
            # submodule rather than a symbol in the package's __init__ file.
            for count in range(len(members) - 1, -1, -1):
                candidate = '.'.join([v for v in [module] + members[:count] if v])
                target = self.module(path, candidate, level)
                if target is None:
                    continue
                if not _test_support(target):
                    return None, False
                target_unit = self.unit(target)
                remaining = members[count:]
                if target_unit and len(remaining) == 1:
                    for node in target_unit[0].body:
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == remaining[0]:
                            return (target, node), False
                return None, True
            # An explicitly imported product API is not an unknown test helper.
            return None, (module.split('.')[0] in ('pytest', 'unittest')
                          or _test_support(module.replace('.', '/') + '.py'))
        if len(pieces) == 1:
            for parent in PurePosixPath(path).parents:
                target = str(parent / 'conftest.py')
                target_unit = self.unit(target) if target in self.texts and target != path else None
                if target_unit:
                    for node in target_unit[0].body:
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                            return (target, node), False
        return None, True

    def assertions(self, path, function, depth=0, seen=()):
        key = (path, function.lineno)
        if key in seen:
            return []
        assertions = []
        for node in _body_nodes(function):
            if isinstance(node, ast.Assert):
                assertions.append(not _constant(node.test))
            elif isinstance(node, ast.Call):
                name = _name(node.func)
                last = name.rsplit('.', 1)[-1]
                target, fallback = self.resolve(path, function, name)
                if target:
                    if depth < 3 and isinstance(target[1], (ast.FunctionDef, ast.AsyncFunctionDef)):
                        assertions.extend(self.assertions(*target, depth=depth + 1, seen=seen + (key,)))
                elif fallback:
                    if last.startswith('assert') or last in ('raises', 'warns', 'fail'):
                        assertions.append(not node.args and not node.keywords
                                          or last in ('raises', 'warns', 'assertRaises', 'assertRaisesRegex',
                                                   'assertWarns', 'fail')
                                          or last.startswith(('assert_called', 'assert_not_called'))
                                          or any(not _constant(v) for v in node.args)
                                          or any(not _constant(v.value) for v in node.keywords))
                    elif last.startswith(('check', 'verify', 'expect', 'ensure')):
                        assertions.append(True)
            if any(assertions):
                return [True]
        return assertions

    def callee(self, path, node):
        name = _name(node)
        root, _, member = name.partition('.')
        imported = self.unit(path)[2].get(root)
        if imported:
            module, symbol, level = imported
            if not level:
                return '.'.join(v for v in (module, symbol, member) if v)
        return name

    def availability(self, path, condition, seen=()):
        if _constant(condition):
            return False
        name = self.callee(path, condition)
        if re.search(r'(?:^|\.)(?:os|sys|platform|environ|getenv|which|find_spec|git_environment)'
                     r'(?:\.|$)|(?:available|installed|HAS_|HAVE_)', name):
            return True
        name = _name(condition)
        unit = self.unit(path)
        if unit and name and name not in seen and len(seen) < 3:
            if any(self.availability(path, value, seen + (name,)) for value in unit[3].get(name, [])):
                return True
        return any(self.availability(path, child, seen) for child in ast.iter_child_nodes(condition))


def _conditional_marker(decorator, name):
    if not isinstance(decorator, ast.Call) or name.rsplit('.', 1)[-1] not in ('skipIf', 'skipUnless', 'skipif'):
        return False
    condition = decorator.args[0] if decorator.args else next(
        (v.value for v in decorator.keywords if v.arg == 'condition'), None)
    return condition is not None and not _constant(condition)


def _conditional_runtime(path, node, parents, helpers):
    child = node
    parent = parents.get(child)
    while parent is not None and not isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        if isinstance(parent, ast.If) and child in parent.body + parent.orelse and helpers.availability(path, parent.test):
            return True
        child, parent = parent, parents.get(parent)
    return False


def _python(path, text, add, helpers=None):
    # The gate's parser strips docstrings; parsing fails explicitly on syntax
    # unsupported by the running interpreter rather than falling back to regex.
    _python_code(text)
    helpers = helpers or _PythonAssertions({path: text})
    unit = helpers.unit(path)
    if unit is None:
        raise ValueError('source_parse')
    tree, parents = unit[:2]
    comments = [(token.start[0], token.string) for token in
                tokenize.generate_tokens(io.StringIO(text).readline)
                if token.type == tokenize.COMMENT]
    _comments(comments, 'python', add)
    imports, exports = [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend((v.name.split('.')[0], node.lineno) for v in node.names)
        elif isinstance(node, ast.ImportFrom) and not node.level and node.module:
            imports.append((node.module.split('.')[0], node.lineno))
        if isinstance(node, ast.Call):
            name = helpers.callee(path, node.func)
            parent = parents.get(node)
            decorator = isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node in parent.decorator_list
            if not decorator and re.search(r'(?:^|\.)(?:skipTest|skip|xfail)$', name):
                conditional = name.rsplit('.', 1)[-1] != 'xfail' and _conditional_runtime(path, node, parents, helpers)
                add('skipped_test', node.lineno, 'conditional_skip' if conditional else 'runtime_skip')
        if isinstance(node, ast.ClassDef):
            for decorator in node.decorator_list:
                name = helpers.callee(path, decorator)
                if re.search(r'(?:^|\.)(?:skip\w*|xfail|only)$', name):
                    add('skipped_test', decorator.lineno, 'conditional_skip' if _conditional_marker(decorator, name) else 'test_marker')
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        parent = parents.get(node)
        unittest_method = isinstance(parent, ast.ClassDef) and any(
            _name(v).endswith('TestCase') for v in parent.bases)
        test = node.name.startswith('test_') or node.name == 'test' or unittest_method and node.name.startswith('test')
        decorators = [helpers.callee(path, v) for v in node.decorator_list]
        for decorator, name in zip(node.decorator_list, decorators):
            if re.search(r'(?:^|\.)(?:skip\w*|xfail|only)$', name):
                add('skipped_test', decorator.lineno, 'conditional_skip' if _conditional_marker(decorator, name) else 'test_marker')
        body = list(_body_nodes(node))
        if test:
            assertions = helpers.assertions(path, node)
            if not any(assertions):
                add('fake_test', node.lineno, 'constant_assertions' if assertions else 'no_assertions')
        parent = parents.get(node)
        protocol = isinstance(parent, ast.ClassDef) and any(
            _name(v).rsplit('.', 1)[-1] == 'Protocol' for v in parent.bases)
        exempt = protocol or any(v.rsplit('.', 1)[-1] in ('abstractmethod', 'overload') for v in decorators)
        if not exempt:
            raises = [v for v in body if isinstance(v, ast.Raise)
                      and _name(v.exc).rsplit('.', 1)[-1] in ('NotImplementedError', 'NotImplemented')]
            if raises:
                add('stub', raises[0].lineno, 'not_implemented')
            else:
                documentation = ast.get_docstring(node)
                statements = node.body[1:] if documentation is not None else node.body
                empty = all(isinstance(v, ast.Pass) or isinstance(v, ast.Expr)
                            and isinstance(v.value, ast.Constant) and v.value.value is Ellipsis
                            or isinstance(v, ast.Return) and (v.value is None
                                or isinstance(v.value, ast.Constant) and v.value.value is None)
                            for v in statements)
                marker = TODO.search(documentation or '') or any(
                    node.lineno <= line <= getattr(node, 'end_lineno', node.lineno)
                    and TODO.search(value) for line, value in comments)
                if empty and marker:
                    add('stub', statements[0].lineno if statements else node.lineno, 'todo_body')
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith(('_', 'test')):
                exports.append((node.name, node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            exports.extend((v.id, v.lineno) for target in targets for v in ast.walk(target)
                           if isinstance(v, ast.Name) and not v.id.startswith('_'))
    # Identifier references deliberately omit comments and strings.
    names = Counter(v.id for v in ast.walk(tree) if isinstance(v, ast.Name))
    names.update(v.attr for v in ast.walk(tree) if isinstance(v, ast.Attribute))
    names.update(v.name.rsplit('.', 1)[-1] for v in ast.walk(tree) if isinstance(v, ast.alias))
    names.update(v.name for v in ast.walk(tree) if isinstance(v, (ast.FunctionDef, ast.ClassDef)))
    return imports, exports, set(names)


def _comments(comments, language, add):
    for line, raw in comments:
        for offset, value in enumerate(raw.split('\n')):
            if SUPPRESSION.search(value):
                add('suppression', line + offset, 'checker_ignore')
            pattern = (r'^\s*#\s*(?:async\s+)?def\s+test\w*\s*\(' if language == 'python'
                       else r'^\s*(?://|/\*|\*)?\s*(?:(?:it|test|describe)\s*(?:\.(?:skip|only))?\s*\('
                            r'|(?:func|function)\s+test\w*\s*\()')
            if re.search(pattern, value):
                add('skipped_test', line + offset, 'commented_test')


def _pairs(tokens):
    pairs, stack = {}, []
    for i, (_, value, _, _) in enumerate(tokens):
        if tokens[i][0] == 'literal':
            continue
        if value in ('(', '[', '{'):
            stack.append((value, i))
        elif value in (')', ']', '}') and stack:
            opening, start = stack.pop()
            if opening != {')': '(', ']': '[', '}': '{'}[value]:
                raise ValueError('unbalanced_source')
            pairs[start] = i
    if stack:
        raise ValueError('unbalanced_source')
    return pairs


def _js_dynamic(tokens):
    constants = {'true', 'false', 'null', 'undefined', 'NaN', 'Infinity'}
    for i, (kind, value, _, _) in enumerate(tokens):
        if kind == 'literal' and value.startswith('`') and '${' in value:
            return True
        if kind == 'code' and re.fullmatch(r'[A-Za-z_$][\w$]*', value) and value not in constants:
            # Literal object keys do not read product state; shorthand keys do.
            if i + 1 < len(tokens) and tokens[i + 1][1] == ':' and (i == 0 or tokens[i - 1][1] in ('{', ',')):
                continue
            return True
    return False


def _js_assertions(tokens, pairs, start, end):
    assertions = []
    tap = {'is', 'not', 'equal', 'equals', 'same', 'deepEqual', 'notDeepEqual',
           'ok', 'notOk', 'true', 'false', 'truthy', 'falsy', 'throws',
           'throwsAsync', 'notThrows', 'notThrowsAsync', 'pass', 'fail', 'snapshot'}
    for i in range(start, end):
        value = tokens[i][1]
        if tokens[i][0] != 'code':
            continue
        framework = value == 't' and i + 2 < end and tokens[i + 1][1] == '.' and tokens[i + 2][1] in tap
        if value in ('expect', 'assert') or re.match(r'^assert[A-Z_]', value) or framework:
            j = i + 1
            while j + 1 < end and tokens[j][1] == '.' and tokens[j + 1][0] == 'code':
                j += 2
            if j >= end or tokens[j][1] != '(' or j not in pairs:
                continue
            if value == 'expect' and any(t[1] in ('assertions', 'hasAssertions') for t in tokens[i:j]):
                continue
            close = pairs[j]
            helper = any(t[1] in ('throws', 'rejects', 'doesNotThrow', 'doesNotReject',
                                  'throwsAsync', 'notThrows', 'notThrowsAsync', 'fail') for t in tokens[i:j])
            dynamic = helper or _js_dynamic(tokens[j + 1:close])
            matcher_found = value != 'expect'
            # Include matcher arguments; toThrow asserts by raising even on a
            # constant function expression. expect.assertions is only a counter.
            k = close + 1
            while k + 2 < end and tokens[k][1] == '.':
                matcher = tokens[k + 1][1]
                k += 2
                if tokens[k][1] == '(' and k in pairs:
                    matcher_found = True
                    dynamic = dynamic or matcher.startswith(('toThrow', 'toRaise')) or _js_dynamic(tokens[k + 1:pairs[k]])
                    k = pairs[k] + 1
            if matcher_found:
                assertions.append(dynamic)
    return assertions


def _js_function_start(code, tokens, parens, opening):
    """Locate the declaration so a preceding doc block belongs to this function."""
    before = code[:opening]
    signature = re.search(r'\)\s*(?::\s*[\w<>\[\]| ]+)?\s*(?:=>\s*)?$', before)
    if signature:
        parameter = parens.get(signature.start())
        if parameter is None:
            return None
        start = tokens[parameter][2]
    else:
        # An arrow can omit parentheses around its single parameter.
        signature = re.search(r'[A-Za-z_$][\w$]*\s*=>\s*$', before)
        if not signature:
            return None
        start = signature.start()
    declaration = re.search(
        r'(?<![\w$])(?:(?:export|default|async|static|public|private|protected|override|abstract|declare)\s+)*'
        r'(?:function\s*\*?\s*[\w$]*|'
        r'(?:const|let|var)\s+[\w$]+\s*=\s*(?:async\s*)?(?:function\s*\*?\s*[\w$]*)?|'
        r'(?:(?:get|set)\s+)?[\w$]+)'
        r'(?:\s*<[^;{}()]*>)?\s*$', code[:start])
    if not declaration:
        return start if '=>' in signature[0] else None
    if declaration[0].strip() in ('if', 'for', 'while', 'switch', 'catch', 'with'):
        return None
    return declaration.start()


class _JavascriptAssertions:
    """Resolve local and test-support callbacks with a three-call depth cap."""

    def __init__(self, texts):
        self.texts, self.units = texts, {}

    def unit(self, path, parsed=None):
        if path not in self.units:
            try:
                code, _, tokens = parsed or _lex(self.texts[path], PurePosixPath(path).suffix)
                pairs = _pairs(tokens)
                offsets = {t[2]: i for i, t in enumerate(tokens)}
                functions, imports = [], {}
                # Named declarations and function-valued variables, including
                # expression arrows. Literal/comment contents are masked in code.
                pattern = (r'\b(?:function\s+([\w$]+)\s*\(|'
                           r'(?:const|let|var)\s+([\w$]+)(?:\s*:\s*[^=;]+)?\s*=\s*'
                           r'(?:async\s*)?(?:function\s*[\w$]*\s*)?\()')
                for match in re.finditer(pattern, code):
                    opening = offsets.get(match.end() - 1)
                    if opening not in pairs:
                        continue
                    close = pairs[opening]
                    body = close + 1
                    while body < len(tokens) and tokens[body][1] not in ('{', '=>', ';', '='):
                        body += 1
                    if body < len(tokens) and tokens[body][1] == '=>':
                        body += 1
                    elif body >= len(tokens) or tokens[body][1] != '{':
                        continue
                    end = self.end(tokens, pairs, body)
                    functions.append((match[1] or match[2], offsets[match.start()], body, end))
                for match in re.finditer(r'\b(?:const|let|var)\s+([\w$]+)\s*=\s*(?:async\s+)?[\w$]+\s*=>', code):
                    body = offsets[match.end() - 2] + 1
                    functions.append((match[1], offsets[match.start()], body, self.end(tokens, pairs, body)))
                for i, (kind, value, start, _) in enumerate(tokens):
                    if kind != 'literal':
                        continue
                    prefix = code[:start]
                    match = re.search(r'\bimport\s+([^;\n]+?)\s+from\s*$', prefix)
                    require = re.search(r'\b(?:const|let|var)\s+(\{[^}]+\}|[\w$]+)\s*=\s*require\s*\(\s*$', prefix)
                    if not match and not require:
                        continue
                    binding = (match or require)[1].strip()
                    spec = value[1:-1]
                    named = re.search(r'\{([^}]+)\}', binding)
                    if named:
                        for item in named[1].split(','):
                            names = re.split(r'\s+(?:as)\s+|\s*:\s*', item.strip())
                            if names[0]:
                                imports[names[-1]] = (spec, names[0])
                    namespace = re.search(r'\*\s+as\s+([\w$]+)', binding)
                    if namespace:
                        imports[namespace[1]] = (spec, '')
                    elif not binding.startswith('{'):
                        imports[binding.split(',')[0].strip()] = (spec, '' if require else 'default')
                self.units[path] = (tokens, pairs, functions, imports)
            except (ValueError, RecursionError):
                self.units[path] = None
        return self.units[path]

    @staticmethod
    def end(tokens, pairs, body):
        if body in pairs and tokens[body][1] == '{':
            return pairs[body]
        end = body
        while end < len(tokens) and tokens[end][1] not in (';', ',', '}', ')'):
            end = pairs.get(end, end) + 1
        return end

    def module(self, path, spec):
        if spec.startswith('.'):
            parts = list(PurePosixPath(path).parent.parts)
            for part in spec.split('/'):
                if part == '..':
                    if not parts:
                        return None
                    parts.pop()
                elif part != '.':
                    parts.append(part)
            stem = '/'.join(parts)
        else:
            stem = spec
        candidates = [stem] + [stem + suffix for suffix in ('.js', '.ts', '.tsx', '.jsx', '.mjs', '.cjs')]
        candidates.extend(stem + '/index' + suffix for suffix in ('.js', '.ts'))
        return next((p for p in candidates if p in self.texts), None)

    def assertions(self, path, start, end, depth=0, seen=()):
        unit = self.unit(path)
        if unit is None or (path, start, end) in seen:
            return []
        tokens, pairs, functions, imports = unit
        # Declarations do not assert until called. Leave framework callbacks
        # (e.g. expect(...).toThrow()) intact.
        nested = [(decl, stop + 1) for _, decl, _, stop in functions if start <= decl < end]
        visible = [t if not any(a <= i < b for a, b in nested) else ('code', ';', t[2], t[3])
                   for i, t in enumerate(tokens)]
        calls = []
        for i in range(start, end):
            if visible[i][0] != 'code' or not re.fullmatch(r'[A-Za-z_$][\w$]*', visible[i][1]):
                continue
            if i and visible[i - 1][1] == '.':
                continue
            name, j = visible[i][1], i + 1
            while j + 1 < end and visible[j][1] == '.':
                name += '.' + visible[j + 1][1]
                j += 2
            if j >= end or visible[j][1] != '(':
                continue
            target, target_path, known = None, path, False
            candidates = [f for f in functions if f[0] == name and not any(
                tokens[a][1] == '{' and a < f[1] < b and not a < i < b for a, b in pairs.items())]
            if candidates:
                target = max(candidates, key=lambda f: f[1])
                known = True
            root, _, member = name.partition('.')
            if root in imports:
                spec, symbol = imports[root]
                target_path = self.module(path, spec)
                known = True
                if target_path and _test_support(target_path):
                    other = self.unit(target_path)
                    wanted = member or symbol
                    if other:
                        target = next((f for f in other[2] if f[0] == wanted), None)
                        if symbol == 'default' and not member:
                            target = next((f for f in other[2] if re.search(
                                r'\bexport\s+default\s*$', self.texts[target_path][:other[0][f[1]][2]])), target)
                elif not target_path and _test_support(spec):
                    known = False
            framework = root in imports and imports[root][0] in (
                'vitest', '@jest/globals', '@playwright/test', 'bun:test', 'expect',
                'chai', 'assert', 'assert/strict', 'node:assert', 'node:assert/strict')
            if framework:
                if imports[root][1] in ('expect', 'assert'):
                    visible[i] = ('code', imports[root][1], visible[i][2], visible[i][3])
                continue
            if target or known:
                for k in range(i, j):
                    visible[k] = ('code', ';', visible[k][2], visible[k][3])
            if target_path and _test_support(target_path):
                known = target is not None
            calls.append((name, target, target_path, known, pairs.get(j) == j + 1))
        assertions = _js_assertions(visible, pairs, start, end)
        for name, target, target_path, known, no_args in calls:
            if target and depth < 3:
                body, stop = target[2:]
                other = self.unit(target_path)
                beginning = body + 1 if other[0][body][1] == '{' else body
                assertions.extend(self.assertions(target_path, beginning, stop, depth + 1,
                                                  seen + ((path, start, end),)))
            elif not known and name.rsplit('.', 1)[-1].startswith(('check', 'verify', 'expect', 'ensure', 'assert')):
                # Framework expect/assert forms retain their constant checks.
                if (not re.match(r'^(?:expect|assert)(?:\.|$)|^assert[A-Z_]', name)
                        or no_args and name.rsplit('.', 1)[-1].startswith('assert') and name != 'assert'):
                    assertions.append(True)
        return assertions


def _javascript(path, text, add, helpers=None):
    suffix = PurePosixPath(path).suffix.lower()
    code, comments, tokens = _lex(text, suffix)
    line = lambda offset: text.count('\n', 0, offset) + 1
    _comments([(line(start), raw) for start, raw in comments], 'javascript', add)
    pairs = _pairs(tokens)
    helpers = helpers or _JavascriptAssertions({path: text})
    helpers.unit(path, (code, comments, tokens))
    parens = {tokens[end][2]: start for start, end in pairs.items() if tokens[start][1] == '('}
    for match in re.finditer(r'\b(?:(?:it|test|describe)\s*\.\s*(?:skip|only|todo)|'
                             r'(?:xit|xtest|xdescribe|fit|ftest|fdescribe))\s*(?=\(|\.)', code):
        add('skipped_test', line(match.start()), 'runner_marker')
    for match in re.finditer(r'\b(?:it|test|xit|xtest|fit|ftest)\b(?:\s*\.\s*\w+)*\s*\(', code):
        opening = next((i for i, t in enumerate(tokens) if t[2] == match.end() - 1), None)
        if opening is None or opening not in pairs:
            continue
        close = pairs[opening]
        # .each(data)(title, callback) has a second call.
        if close + 1 < len(tokens) and tokens[close + 1][1] == '(':
            opening = close + 1
            close = pairs.get(opening, close)
        callback = next((i for i in range(opening + 1, close)
                         if tokens[i][0] == 'code' and tokens[i][1] in ('=>', 'function')), None)
        if callback is None:
            continue
        if tokens[callback][1] == '=>':
            body = callback + 1 if tokens[callback + 1][1] == '{' else callback
        else:
            body = next((i for i in range(callback + 1, close) if tokens[i][1] == '{'
                         and tokens[i][0] == 'code'), callback)
        end = pairs.get(body, close)
        assertions = helpers.assertions(path, body + 1, end)
        if not any(assertions):
            add('fake_test', line(match.start()), 'constant_assertions' if assertions else 'no_assertions')
    for match in re.finditer(r'\bthrow\s+(?:new\s+)?(?:NotImplementedError\b|Error\s*\()', code):
        tail = next((i for i, t in enumerate(tokens) if t[2] >= match.start()), len(tokens))
        nearby = tokens[tail:tail + 8]
        if 'NotImplementedError' in match[0] or any(t[0] == 'literal' and
                re.search(r'not\s+implemented', t[1], re.I) for t in nearby):
            add('stub', line(match.start()), 'not_implemented')
    # Function/method bodies containing only placeholders and their own marker.
    for i, token in enumerate(tokens):
        if token[0] != 'code' or token[1] != '{' or i not in pairs:
            continue
        end = pairs[i]
        declaration = _js_function_start(code, tokens, parens, token[2])
        if declaration is None:
            continue
        # Interface/type declarations have requirements, not implementations.
        if any(tokens[j][1] in ('interface', 'type') and tokens[j][0] == 'code'
               for j in range(max(0, i - 3), i)):
            continue
        if re.match(r'(?:(?:export|default|async|static|public|private|protected|override)\s+)*'
                    r'(?:abstract|declare)\b', code[declaration:token[2]]):
            continue
        body = tokens[i + 1:end]
        values = [t[1] for t in body if t[1] != ';']
        empty = all(t[0] == 'code' for t in body) and values in (
            [], ['.', '.', '.'], ['return'], ['return', 'null'], ['return', 'undefined'])
        marker = any(TODO.search(raw) and (
            token[2] < start < tokens[end][2]
            or raw.startswith('/**') and start + len(raw) <= declaration
            and not text[start + len(raw):declaration].strip()) for start, raw in comments)
        if empty and marker:
            add('stub', line(body[0][2] if body else declaration), 'todo_body')
    imports = []
    for i, (kind, value, start, _) in enumerate(tokens):
        if kind != 'literal':
            continue
        prefix = code[max(0, start - 400):start]
        if re.search(r'(?:\bfrom\s*|\b(?:require|import)\s*\(\s*|\bimport\s*)$', prefix):
            spec = value[1:-1]
            if not spec.startswith(('.', '/', '#')):
                root = '/'.join(spec.split('/')[:2]) if spec.startswith('@') else spec.split('/')[0]
                imports.append((root, line(start)))
    exports = [(m[1], line(m.start())) for m in re.finditer(
        r'\bexport\s+(?:default\s+)?(?:async\s+)?(?:function|class|const|let|var|enum|interface|type)\s+([\w$]+)', code)]
    exports.extend((m[1], line(m.start())) for m in re.finditer(r'\bexports\.([\w$]+)\s*=', code))
    for match in re.finditer(r'\bexport\s*\{([^}]+)\}', code):
        # A re-export names the public symbol in this file as well.
        for item in match[1].split(','):
            names = re.findall(r'[A-Za-z_$][\w$]*', item)
            if names:
                exports.append((names[-1], line(match.start())))
    exports.extend((m[1], line(m.start())) for m in re.finditer(r'\bmodule\.exports\s*=\s*([A-Za-z_$][\w$]*)\s*[;\n]', code))
    return imports, exports, {t[1] for t in tokens if t[0] == 'code'}


def _swift(path, text, add):
    code, comments, tokens = _lex(text, '.swift')
    line = lambda offset: text.count('\n', 0, offset) + 1
    pairs = _pairs(tokens)
    _comments([(line(start), raw) for start, raw in comments], 'swift', add)
    for match in re.finditer(r'\bXCTSkip(?:If|Unless)?\s*\(', code):
        opening = next((i for i, t in enumerate(tokens) if t[2] == match.end() - 1), None)
        conditional = False
        if opening in pairs and not re.match(r'XCTSkip\s*\(', match[0]):
            end = pairs[opening]
            comma = next((i for i in range(opening + 1, end) if tokens[i][1] == ','), end)
            conditional = _js_dynamic(tokens[opening + 1:comma])
        add('skipped_test', line(match.start()), 'conditional_skip' if conditional else 'runtime_skip')
    for i, t in enumerate(tokens):
        if t[0] == 'code' and t[1] in ('fatalError', 'preconditionFailure'):
            # Preconditions/guards describe invariants and are not stubs.
            if t[1] != 'fatalError':
                continue
            enclosing = [j for j, end in pairs.items() if tokens[j][1] == '{' and j < i < end]
            if any(re.search(r'\bguard\b[^{}]*\belse\s*$', code[:tokens[j][2]]) for j in enclosing):
                continue
            nearby = tokens[i + 1:i + 4]
            if any(v[0] == 'literal' and re.search(r'not\s+implemented', v[1], re.I) for v in nearby):
                add('stub', line(t[2]), 'not_implemented')
    for match in re.finditer(r'\bfunc\s+\w+\s*\([^{}]*\)[^{\n]*\{', code):
        i = next((i for i, t in enumerate(tokens) if t[2] == match.end() - 1), None)
        if i is not None and i in pairs and pairs[i] == i + 1 and any(
                tokens[i][2] < start < tokens[pairs[i]][2] and TODO.search(raw) for start, raw in comments):
            add('stub', line(match.start()), 'todo_body')
    return [], [], set()


def _normalize(name):
    return re.sub(r'[-_.]+', '-', name).lower()


def _requirements(text):
    return [m[1] for value in text.splitlines() for m in
            [re.match(r'\s*([A-Za-z0-9][\w.-]*)(?:\[.*?\])?(?:\s*[<>=!~;@]|\s*(?:#.*)?$)', value)] if m]


def _manifest(path, text):
    """Return all declarations, runtime declarations and local package names."""
    name = PurePosixPath(path).name
    declared, runtime, local = set(), set(), set()
    if name == 'package.json':
        data = intake._json(text)
        for key in ('dependencies', 'devDependencies', 'peerDependencies', 'optionalDependencies'):
            values = data.get(key, {})
            if not isinstance(values, dict):
                raise ValueError('dependencies')
            declared.update(values)
            if key in ('dependencies', 'optionalDependencies'):
                runtime.update(values)
        if isinstance(data.get('name'), str):
            local.add(data['name'])
    elif name.startswith('requirements'):
        declared.update(_requirements(text))
        if not re.search(r'dev|test|lint', name, re.I):
            runtime.update(declared)
    elif name == 'setup.cfg':
        data = configparser.ConfigParser(interpolation=None)
        data.read_string(text)
        if data.has_option('options', 'install_requires'):
            runtime.update(_requirements(data.get('options', 'install_requires')))
        declared.update(runtime)
        if data.has_section('options.extras_require'):
            for value in data['options.extras_require'].values():
                declared.update(_requirements(value))
        if data.has_option('metadata', 'name'):
            local.add(data.get('metadata', 'name').replace('-', '_'))
    else:
        # Normalize multiline arrays for intake's Python 3.9 conservative TOML
        # reader. Only dependency/name tables are needed; other TOML is opaque.
        section, pending = '', ''
        for raw in text.splitlines():
            # tokenize-like removal of TOML comments, preserving quoted '#'.
            clean = re.sub(r'''("(?:\\.|[^"\\])*"|'[^']*')|#.*''',
                           lambda m: m[1] or '', raw).strip()
            if not clean:
                continue
            if clean.startswith('[') and not pending:
                section = clean.strip('[] ')
                continue
            if pending:
                clean = pending + ' ' + clean
            if clean.count('[') > clean.count(']'):
                pending = clean
                continue
            pending = ''
            if '=' not in clean:
                continue
            key, value = (v.strip() for v in clean.split('=', 1))
            key = key.strip('\'"')
            if section in ('project', 'tool.poetry') and key == 'name':
                local.add(value.strip('\'"').replace('-', '_'))
            if section == 'project' and key == 'dependencies' or section.startswith('project.optional-dependencies'):
                # PEP 508 names are the leading identifiers of literal strings.
                values = re.findall(r'''["']([^"']+)["']''', value)
                names = _requirements('\n'.join(values))
                declared.update(names)
                if section == 'project':
                    runtime.update(names)
            elif section.startswith('tool.poetry.') and section.endswith('dependencies') and key != 'python':
                declared.add(key)
                if section == 'tool.poetry.dependencies':
                    runtime.add(key)
        if pending:
            raise ValueError('unterminated_dependencies')
    normalize = str.lower if name == 'package.json' else _normalize
    return {normalize(v) for v in declared}, {normalize(v) for v in runtime}, local


def _dependencies(sources, manifests, add_for):
    local = set()
    for path in sources:
        p = PurePosixPath(path)
        if _language(path) != 'swift':
            local.update(p.parts[:-1])
        if p.suffix == '.py':
            local.add(p.stem)
    for _, (_, _, names) in manifests.items():
        local.update(names)
    used = set()
    for path, (imports, _, _) in sources.items():
        language = _language(path)
        if language == 'swift':
            continue
        candidates = [p for p in manifests if PurePosixPath(p).parent in PurePosixPath(path).parents
                      and (PurePosixPath(p).name == 'package.json') == (language != 'python')]
        depth = max((len(PurePosixPath(p).parent.parts) for p in candidates), default=-1)
        nearest = [p for p in candidates if len(PurePosixPath(p).parent.parts) == depth]
        declared = set().union(*(manifests[p][0] for p in nearest))
        seen = set()
        for root, line in imports:
            distribution = _normalize(DISTRIBUTIONS.get(root.lower(), root)) if language == 'python' else root.lower()
            used.add((language == 'python', distribution))
            builtin = (root in PY_STDLIB or root in sys.builtin_module_names) if language == 'python' else root.startswith('node:') or root.split('/')[0] in NODE_BUILTINS
            if builtin or root in local or distribution in declared or root in seen:
                continue
            seen.add(root)
            add_for(path, 'dependency', line, 'undeclared_import')
    for path, (_, runtime, _) in sorted(manifests.items()):
        python = PurePosixPath(path).name != 'package.json'
        for name in sorted(runtime):
            if (python, name) not in used:
                add_for(path, 'dependency', 0, 'unused_runtime:' + name)


def scan(repo):
    """Return main worktree and deterministic findings from its selected HEAD."""
    started = time.monotonic()
    deadline = started + 9.5
    main, discovery = intake.scan(repo)
    repo = intake.repository_root(repo, deadline)
    head = discovery['meta']['head']
    unseen = list(discovery['meta']['unseen'])
    entries = {}
    if head:
        _, raw = intake._git(repo, 'ls-tree', '--full-tree', '-r', '-l', '-z', head, deadline=deadline)
        for record in raw.split(b'\0')[:-1]:
            header, path = record.split(b'\t', 1)
            mode, _, oid, size = header.split()
            path = os.fsdecode(path)
            if not intake._own_state(path):
                entries[path] = (mode.decode(), oid.decode(), 0 if size == b'-' else int(size))
    blobs = _fetch(repo, entries, unseen, deadline)
    texts, sources, manifests, findings = {}, {}, {}, []

    def add_for(path, kind, line, signal):
        lines = texts.get(path, '').split('\n')
        evidence = intake._clean(lines[line - 1].strip())[:160] if 0 < line <= len(lines) else ''
        findings.append(dict(class_=kind, path=path, line=line, language=_language(path) or 'none',
                             signal=signal, evidence=evidence, advisory=kind == 'dead_code' or signal == 'conditional_skip'))

    for path, raw in sorted(blobs.items()):
        try:
            texts[path] = raw.decode('utf-8-sig')
        except UnicodeError:
            unseen.append({'path': path, 'reason': 'malformed_or_unsupported'})
    python_helpers = _PythonAssertions(texts)
    js_helpers = _JavascriptAssertions(texts)
    for path, text in sorted(texts.items()):
        try:
            add = lambda kind, line, signal: add_for(path, kind, line, signal)
            language = _language(path)
            if language:
                parser = {'python': _python, 'javascript': _javascript, 'typescript': _javascript, 'swift': _swift}[language]
                # Malformed syntax cannot leave partially trustworthy findings.
                before = len(findings)
                try:
                    sources[path] = (parser(path, text, add, python_helpers) if language == 'python'
                                     else parser(path, text, add, js_helpers) if language in ('javascript', 'typescript')
                                     else parser(path, text, add))
                except (SyntaxError, ValueError, tokenize.TokenError, RecursionError):
                    del findings[before:]
                    raise ValueError('source_parse')
            else:
                manifests[path] = _manifest(path, text)
        except (UnicodeError, ValueError, RecursionError, configparser.Error):
            unseen.append({'path': path, 'reason': 'malformed_or_unsupported'})
    references = Counter(name for _, _, names in sources.values() for name in names)
    for path, (_, exports, names) in sources.items():
        for name, line in exports:
            if references[name] - int(name in names) == 0:
                add_for(path, 'dead_code', line, 'unreferenced_export:' + name)
    _dependencies(sources, manifests, add_for)
    kinds = {v['kind'] for v in discovery['commands']}
    for kind, present in (('test', 'test' in kinds), ('lint', 'lint' in kinds),
                          ('ci', bool(discovery['ci']['workflow_files']))):
        if not present:
            add_for('', 'missing_checks', 0, 'no_' + kind)
    other = Counter(PurePosixPath(p).suffix.lower() or '[none]' for p in entries
                    if not _language(p) and not intake._excluded(p) and not intake._secret(p))
    unseen.extend({'extension': ext, 'count': count, 'reason': 'unsupported_extension'}
                  for ext, count in sorted(other.items()))
    for item in findings:
        item['class'] = item.pop('class_')
    # A declaration/call can have overlapping syntactic recognition paths.
    unique = {(v['class'], v['path'], v['line'], v['signal']): v for v in findings}
    findings = [unique[key] for key in sorted(unique)]
    unseen = [json.loads(v) for v in sorted({json.dumps(v, sort_keys=True) for v in unseen})]
    report = dict(schema='orcexe.slop.v1', head=head,
                  counts={kind: sum(v['class'] == kind for v in findings) for kind in CLASSES},
                  findings=findings, unseen=unseen,
                  meta={'duration_ms': round((time.monotonic() - started) * 1000),
                        'generated_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')})
    report = intake._clean(report)
    report['findings'].sort(key=lambda v: (v['class'], v['path'], v['line'], v['signal']))
    return main, report


def _markdown(report):
    lines = ['# Slop report', '', '| Class | Count |', '| --- | ---: |']
    lines.extend('| {} | {} |'.format(kind, report['counts'][kind]) for kind in CLASSES)
    # Escape untrusted source as text, including markup/backticks and newlines
    # in Git paths. The report must not turn project content into active links.
    def escape(value):
        return (value.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
                .replace('`', '&#96;').replace('[', '&#91;').replace(']', '&#93;')
                .replace('|', '&#124;').replace('\n', '\\n').replace('\r', '\\r'))
    for kind in CLASSES:
        group = [v for v in report['findings'] if v['class'] == kind]
        if group:
            lines.extend(['', '## ' + kind, ''])
            lines.extend('- {}:{} — {}{}: {}'.format(escape(v['path'] or '(repo)'), v['line'],
                         escape(v['signal']), ' (advisory)' if v['advisory'] else '', escape(v['evidence'])) for v in group)
    lines.extend(['', '## Prioritized fix plan', ''])
    actions = {'fake_test': ('standard', 'Assert observable product behavior'),
               'skipped_test': ('fast', 'Review and restore disabled or focused tests'),
               'suppression': ('fast', 'Resolve checker errors and remove silencing'),
               'stub': ('standard', 'Implement incomplete bodies'),
               'dependency': ('fast', 'Reconcile imports and dependency declarations'),
               'missing_checks': ('standard', 'Add the missing test, lint/typecheck or CI checks'),
               'dead_code': ('standard', 'Review unused exports; advisory only')}
    for kind in ('stub', 'fake_test', 'skipped_test', 'dependency', 'suppression', 'missing_checks', 'dead_code'):
        if report['counts'][kind]:
            lane, action = actions[kind]
            lines.append('- {}: {} (lane: {}).'.format(kind, action, lane))
    if not report['findings']:
        lines.append('No findings to prioritize.')
    return '\n'.join(lines) + '\n'


def run_slop(repo):
    main, report = scan(repo)
    directory = main / '.orcexe'
    if directory.is_symlink():
        raise intake.IntakeError('output_symlink')
    directory.mkdir(mode=0o700, exist_ok=True)
    fsio.atomic_write_json(directory / 'slop.json', report, mode=0o600)
    from orcexe import published
    published.report(published.write_text(directory, 'slop-report.md', _markdown(report)))
    return report


def slop_command(args):
    try:
        report = run_slop(args.repo or Path.cwd())
    except (OSError, ValueError, gitutil.GitError):
        print(json.dumps({'error': 'Slop could not inspect repository'}) if args.json
              else 'Slop could not inspect repository')
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) if args.json
          else 'Wrote .orcexe/slop.json and .orcexe/slop-report.md')
    return 0
