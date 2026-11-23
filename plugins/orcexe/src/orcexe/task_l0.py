"""Standard task checks over frozen objects and disposable gate copies."""

import ast
import fnmatch
import os
from difflib import SequenceMatcher
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
import tokenize

from orcexe import gate_lite, isolation, slop
from orcexe.adapters.base import safe_write_bytes, safe_write_json, safe_write_text
from orcexe.envfilter import child_env
from orcexe.redact import redact_text


CHECK_IDS = ('test_count_dropped', 'skip_added', 'tests_missing', 'red_first_missing', 'assertion_missing')


_RUNNER_AGGREGATE = re.compile(
    r'(?m)^Ran (\d+) tests? in \d+ modules? in \d+(?:\.\d+)?s; failures=\d+ errors=\d+ '
    r'skips=(\d+); failed modules=\d+[ \t]*$')


def runner_summary(text):
    """Trusted runner totals; text printed after the real summary cannot inflate them.

    tests/tools/run_tests.py prints only some per-module unittest blocks, then
    one aggregate `Ran N tests in M modules in Xs; failures=F errors=E skips=K;
    failed modules=D`. When exactly one such aggregate appears and no summary of
    any format follows it, it is authoritative (count N, skips K) and the module
    blocks before it are ignored. Two aggregates, or any summary after it, are
    not_seen (count/skips None). Without an aggregate, one summary is used as
    is; with several, the last is trusted only if its count (and skip total,
    when every earlier one is known) equals the sum of the earlier ones.
    """
    summaries = []
    unittest_runs = list(re.finditer(r'(?m)^Ran\s+(\d+)\s+tests?\s+in\s+[^\n]+$', text))
    for index, match in enumerate(unittest_runs):
        end = unittest_runs[index + 1].start() if index + 1 < len(unittest_runs) else len(text)
        aggregate = re.search(r'\bskips=(\d+)\b', match[0])
        outcome = re.search(r'(?m)^(?:OK|FAILED)(?:\s*\(([^\n]*)\))?\s*$', text[match.end():end])
        categories = None
        if aggregate:
            categories = dict(skipped=int(aggregate[1]), xfailed=0, expected_failures=0)
        elif outcome:
            categories = dict(skipped=0, xfailed=0, expected_failures=0)
            categories.update((kind.replace(' ', '_'), int(n)) for kind, n in re.findall(
                r'(skipped|expected failures)=(\d+)', outcome[1] or ''))
        summaries.append((match.start(), int(match[1]), categories))
    for match in re.finditer(r'(?m)^.*\bin\s+\d+(?:\.\d+)?s[^\n]*$', text):
        parts = re.findall(r'(?<!\w)(\d+) (passed|failed|skipped|xfailed|xpassed|deselected|errors?|warnings?)\b', match[0])
        kinds = ('passed', 'failed', 'skipped', 'xfailed', 'xpassed')
        if parts and any(kind in kinds for _, kind in parts):
            summaries.append((match.start(), sum(int(n) for n, k in parts if k in kinds),
                              dict(skipped=sum(int(n) for n, k in parts if k == 'skipped'),
                                   xfailed=sum(int(n) for n, k in parts if k == 'xfailed'), expected_failures=0)))
    unseen = dict(count=None, skips=None, skip_counts=None)
    aggregates = list(_RUNNER_AGGREGATE.finditer(text))
    if aggregates:
        if len(aggregates) > 1 or any(start > aggregates[0].start() for start, _, _ in summaries):
            return unseen
        skips = int(aggregates[0][2])
        return dict(count=int(aggregates[0][1]), skips=skips,
                    skip_counts=dict(skipped=skips, xfailed=0, expected_failures=0))
    if not summaries:
        return unseen
    summaries.sort(key=lambda v: v[0])
    *earlier, (_, count, categories) = summaries
    if earlier:
        if count != sum(n for _, n, _ in earlier):
            return unseen
        if all(c is not None for _, _, c in earlier):
            totals = {key: sum(c[key] for _, _, c in earlier) for key in ('skipped', 'xfailed', 'expected_failures')}
            if categories is not None and (categories['skipped'] + categories['xfailed']
                                           != totals['skipped'] + totals['xfailed']):
                return unseen
            # The earlier blocks carry the category detail an aggregate line omits.
            categories = totals
    return dict(count=count, skips=None if categories is None else sum(categories.values()), skip_counts=categories)


def test_count(text):
    return runner_summary(text)['count']


def _python_tests(path, text, tree=None):
    tree = tree if tree is not None else gate_lite.parse_python(text)
    units = []
    def visit(body, prefix='', eligible=True):
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, prefix + node.name + '.', node.name.startswith('Test') or
                      any(slop._name(base).endswith('TestCase') for base in node.bases))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and eligible and node.name.startswith('test'):
                units.append((prefix + node.name, node))
    visit(tree.body)
    return units


def static_count(checkout):
    """Discover definitions without importing repository code or following links."""
    total = 0
    for folder, dirs, files in os.walk(checkout, followlinks=False):
        dirs[:] = [d for d in dirs if d not in ('.git', 'node_modules', '.venv', 'venv')
                   and not (Path(folder) / d).is_symlink()]
        for name in files:
            path = Path(folder) / name
            if path.is_symlink():
                continue
            if name.endswith('.py') and (fnmatch.fnmatch(name, 'test*.py') or fnmatch.fnmatch(name, '*_test.py')):
                total += len(_python_tests(str(path), path.read_text(encoding='utf-8')))
            elif re.search(r'\.(?:test|spec)\.[cm]?[jt]sx?$', name):
                code, _, _ = slop._lex(path.read_text(encoding='utf-8'), path.suffix)
                total += len(re.findall(r'\b(?:it|test)\s*(?:\.\s*\w+\s*)*\(', code))
    return total


def _python_markers(text, add, path='test_source.py', helpers=None):
    helpers = helpers or slop._PythonAssertions({path: text})
    unit = helpers.unit(path)
    if unit is None:
        raise ValueError('source_parse')
    tree = unit[0]
    name = lambda node: helpers.callee(path, node)
    for node in ast.walk(tree):
        if isinstance(node, ast.Raise) and name(node.exc).rsplit('.', 1)[-1] == 'SkipTest':
            add('skipped_test', node.lineno, 'runtime_skip')
        if isinstance(node, ast.Call) and name(node.func).rsplit('.', 1)[-1] in ('importorskip', 'skip', 'skipTest', 'xfail'):
            add('skipped_test', node.lineno, 'runtime_skip')
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            for decorator in node.decorator_list:
                if name(decorator).rsplit('.', 1)[-1] == 'expectedFailure':
                    add('skipped_test', decorator.lineno, 'test_marker')
    for _, node in _python_tests('', text, tree):
        body = node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            body = body[1:]
        if body and isinstance(body[0], ast.Return) and body[0].value is None:
            add('skipped_test', body[0].lineno, 'hollow_test')
        elif not helpers.assertions(path, node):
            add('skipped_test', node.lineno, 'hollow_test')


def _js_tests(path, text, helpers=None):
    code, _, tokens = slop._lex(text, PurePosixPath(path).suffix)
    pairs = slop._pairs(tokens)
    helpers = helpers or slop._JavascriptAssertions({path: text})
    units, seen = [], {}
    for match in re.finditer(r'\b(?:it|test|xit|xtest|fit|ftest)\b(?:\s*\.\s*\w+)*\s*\(', code):
        opening = next((i for i, t in enumerate(tokens) if t[2] == match.end() - 1), None)
        if opening is None or opening not in pairs:
            continue
        close = pairs[opening]
        if close + 1 < len(tokens) and tokens[close + 1][1] == '(':
            opening = close + 1
            close = pairs.get(opening, close)
        callback = next((i for i in range(opening + 1, close)
                         if tokens[i][0] == 'code' and tokens[i][1] in ('=>', 'function')), None)
        if callback is None:
            continue
        body = callback + 1 if tokens[callback][1] == '=>' else next(
            (i for i in range(callback + 1, close) if tokens[i][1] == '{'), callback)
        end = pairs.get(body, close)
        label = next((t[1] for t in tokens[opening + 1:callback] if t[0] == 'literal'), '')
        seen[label] = seen.get(label, 0) + 1
        visible = tokens[body + 1:end] if tokens[body][1] == '{' else tokens[body:end]
        checks = bool(helpers.assertions(path, body + 1 if tokens[body][1] == '{' else body, end))
        units.append(((label, seen[label]), tuple((t[0], t[1]) for t in tokens[opening:end + 1]),
                      text.count('\n', 0, match.start()) + 1,
                      checks, not checks or bool(visible and visible[0][1] == 'return'
                                                 and (len(visible) == 1 or visible[1][1] == ';'))))
    return units


def _version_sources(observed, blobs, texts, version):
    sources = dict(texts or {})
    for file, pair in observed['versions']:
        sources[file['path']] = blobs.get(pair[version], b'').decode('utf-8')
    return sources


def _assertion_helpers(texts):
    return dict(python=slop._PythonAssertions(texts), javascript=slop._JavascriptAssertions(texts),
                swift=slop._SwiftAssertions(texts))


def _support_sources(checkout):
    """Read bounded support source from a frozen copy, never following links."""
    sources, budget = {}, 0
    for folder, dirs, files in os.walk(checkout, followlinks=False):
        dirs[:] = sorted(d for d in dirs if not (Path(folder) / d).is_symlink()
                         and not slop.intake._excluded((Path(folder) / d).relative_to(checkout).as_posix()))
        for name in sorted(files):
            target = Path(folder) / name
            path = target.relative_to(checkout).as_posix()
            if (target.is_symlink() or slop.intake._secret(path) or not slop._language(path) or not slop._test_support(path)
                    or not slop._wanted(path)):
                continue
            size = target.stat().st_size
            if size > slop.intake.FILE_CAP or budget + size > slop.intake.TOTAL_BUDGET:
                continue
            budget += size
            try:
                sources[path] = target.read_text(encoding='utf-8-sig')
            except UnicodeError:
                continue
    return sources


def _assertion_missing(observed, blobs, texts=None):
    findings = []
    helpers = _assertion_helpers(_version_sources(observed, blobs, texts, 1))
    for file, (old, new) in observed['versions']:
        path = file['path']
        if not is_test(path):
            continue
        before, after = (blobs.get(oid, b'').decode('utf-8') for oid in (old, new))
        if path.endswith('.py'):
            previous = {name: ast.dump(node) for name, node in _python_tests(path, before)}
            unit = helpers['python'].unit(path)
            if unit is None:
                raise ValueError('source_parse')
            for name, node in _python_tests(path, after, unit[0]):
                if previous.get(name) == ast.dump(node):
                    continue
                if not helpers['python'].assertions(path, node):
                    findings.append(dict(path=path, line=node.lineno, signal='no_assertions'))
        elif slop._language(path) in ('javascript', 'typescript'):
            previous = {key: content for key, content, _, _, _ in _js_tests(path, before)}
            for key, content, line, assertion, _ in _js_tests(path, after, helpers['javascript']):
                if previous.get(key) != content and not assertion:
                    findings.append(dict(path=path, line=line, signal='no_assertions'))
        elif slop._language(path) == 'swift':
            previous = {key: content for key, content, _, _, _ in slop._swift_tests(before)}
            for key, content, line, checks, _ in slop._swift_tests(after, path, helpers['swift']):
                if previous.get(key) != content and not checks:
                    findings.append(dict(path=path, line=line, signal='no_assertions'))
    return findings


def is_test(path):
    p = PurePosixPath(path)
    if p.suffix.lower() == '.py' and (p.name.startswith('test') or p.name.endswith('_test.py')):
        return True
    return (p.suffix.lower() in ('.py', '.js', '.jsx', '.mjs', '.cjs', '.ts', '.tsx', '.go', '.rs', '.java', '.swift')
            and (any(v.lower() in ('test', 'tests', '__tests__') for v in p.parts[:-1])
                 or bool(re.search(r'(^test[_.]|[._](?:test|spec)\.|_test\.|Tests?\.)', p.name))))


def is_doc(path):
    p = PurePosixPath(path)
    return (p.suffix.lower() in gate_lite._DOC
            or p.name.upper() in ('LICENSE', 'NOTICE', 'COPYING'))


def docs_only(paths):
    """True only for a non-empty diff whose every path is a doc and none is a test."""
    return bool(paths) and all(is_doc(p) and not is_test(p) for p in paths)


def test_commands(commands):
    return [c for c in commands if re.search(r'(?:^|[-_/ .])(?:test|tests|pytest|unittest)(?:$|[-_/ .])',
                                             c.id + ' ' + ' '.join(c.argv))]


def _tests(commands, checkout, directory, timeout, deadline, scratch, interpreter=None):
    """Keep runner exit evidence separate from timeout, missing and partial output."""
    env, _ = child_env()
    observations = []
    for command in commands:
        unavailable = gate_lite.python_unavailable(command.argv, interpreter)
        if unavailable:
            safe_write_text(directory / (command.id + '.log'), 'not_seen: ' + unavailable + '\n')
            observations.append(dict(id=command.id, count=None, skips=None, skip_counts=None, exit_code=None))
            continue
        lines, size = [], 0
        def capture(line):
            nonlocal size
            size += len(line.encode('utf-8')) + 1
            if size <= 1024 * 1024:
                lines.append(line + '\n')
        observed = isolation.run(gate_lite.python_argv(command.argv, interpreter), cwd=str(checkout), env=env,
                            timeout_s=max(.001, min(timeout, deadline - time.monotonic())),
                            stall_s=None, on_line=capture, on_stderr_line=capture,
                            kill_grace_s=min(.1, timeout), max_line_bytes=1048576)
        complete = (observed.status == 'exited' and not observed.pipes_held
                    and not observed.truncated_lines and size <= 1024 * 1024)
        text = ''.join(lines) if complete else ''
        hint = '; write outside checkout denied' if re.search(
            r'PermissionError|Operation not permitted|Permission denied|Read-only file system', text, re.I) else ''
        safe_write_text(directory / (command.id + '.log'),
                        redact_text(text)[-4096:] + hint if complete else 'not_seen: incomplete\n')
        observations.append(dict(id=command.id, **runner_summary(text),
                                 exit_code=observed.returncode if complete else None))
    return observations


def _skip_added(observed, blobs, texts=None, before_texts=None):
    old_helpers = _assertion_helpers(_version_sources(
        observed, blobs, before_texts if before_texts is not None else texts, 0))
    new_helpers = _assertion_helpers(_version_sources(observed, blobs, texts, 1))
    for file, (old, new) in observed['versions']:
        path = file['path']
        language = slop._language(path)
        if language is None:
            continue
        before = blobs.get(old, b'').decode('utf-8')
        after = blobs.get(new, b'').decode('utf-8')
        old_lines, new_lines = before.splitlines(keepends=True), after.splitlines(keepends=True)
        unchanged = {j + offset + 1: i + offset + 1
                     for i, j, size in SequenceMatcher(None, old_lines, new_lines, autojunk=False).get_matching_blocks()
                     for offset in range(size)}
        parser = {'python': slop._python, 'javascript': slop._javascript,
                  'typescript': slop._javascript, 'swift': slop._swift}[language]
        def markers(text, helpers):
            found = set()
            def add(kind, line, signal):
                if kind == 'skipped_test':
                    found.add((line, signal))
            helper = helpers['javascript' if language == 'typescript' else language]
            parser(path, text, add, helper)
            if language == 'python':
                _python_markers(text, add, path, helper)
            elif language in ('javascript', 'typescript'):
                for _, _, line, _, hollow in _js_tests(path, text, helper):
                    if hollow:
                        add('skipped_test', line, 'hollow_test')
            elif language == 'swift':
                for _, _, line, _, hollow in slop._swift_tests(text, path, helper):
                    if hollow:
                        add('skipped_test', line, 'hollow_test')
            return found
        current = markers(after, new_helpers)
        if not current:
            continue
        previous = markers(before, old_helpers)
        # A multiline marker can begin on an unchanged line. Compare its old
        # finding too, so newly added .only/skip decorators cannot hide there.
        findings = [dict(path=path, line=line, signal=signal) for line, signal in sorted(current)
                    if (unchanged.get(line), signal) not in previous]
        if findings:
            return findings
    return []


@isolation.guarded
def run_l0(*, repo_path, worktree, observed, commands, gate, run_dir,
           command_timeout_s=600, total_timeout_s=1800):
    directory = Path(run_dir) / 'l0'
    deadline = time.monotonic() + total_timeout_s
    checks = []
    def check(identifier, status, detail):
        checks.append(dict(id=identifier, status=status, detail=redact_text(detail)[:1000]))
    try:
        selected = test_commands(gate_lite._commands(commands))
        oids = {oid for _, pair in observed['versions'] for oid in pair if set(oid) != {'0'}}
        blobs = gate_lite._blobs(Path(worktree.path), oids, deadline)
        paths = {f[k] for f in observed['files'] for k in ('path', 'old_path') if f[k]}
        tests = {p for p in paths if is_test(p)}
        cache = Path(os.environ.get('ORCEXE_STATE_DIR') or Path.home() / '.cache/orcexe') / 'gate'
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(dir=str(cache)) as temporary, \
                tempfile.TemporaryDirectory(dir=str(cache)) as overlay:
            base_root = Path(temporary).resolve()
            base = gate_lite._checkout(repo_path, worktree.base_commit, base_root, {}, deadline)
            # F5: the gate's declared interpreter, read from this verified base copy before
            # any test runs in it; probed in the sandbox only when declared.
            interpreter = gate_lite.project_python(repo_path, worktree.base_commit, base, child_env()[0],
                                                   command_timeout_s, deadline, probe_path=False,
                                                   checkout=base) if selected else None
            base_static = static_count(base)
            base_sources = _support_sources(base)
            candidate_root = base_root / 'candidate'
            candidate_root.mkdir()
            candidate_copy = gate_lite._checkout(repo_path, observed['tree'], candidate_root, {}, deadline)
            attempt_static = static_count(candidate_copy)
            candidate_sources = _support_sources(candidate_copy)
            shutil.rmtree(candidate_root)
            counts = _tests(selected, base, directory / 'base', command_timeout_s, deadline, base_root, interpreter)
            candidate = []
            for command in selected:
                # T55: the in-memory copy written by the gate, never the file re-read.
                text = gate_lite.project_log(Path(run_dir) / 'gate', command.id)
                candidate.append(runner_summary(text if text is not None else ''))
            if attempt_static < base_static:
                check('test_count_dropped', 'fail', 'static base=' + str(base_static) + '; candidate=' + str(attempt_static))
            elif (not selected or any(v['count'] is None or v['exit_code'] is None for v in counts)
                    or any(n['count'] is None for n in candidate)):
                check('test_count_dropped', 'not_seen', 'runner_count_not_seen')
            else:
                passed = all(n['count'] >= b['count'] for n, b in zip(candidate, counts))
                check('test_count_dropped', 'pass' if passed else 'fail',
                      'base=' + str([b['count'] for b in counts]) + '; candidate=' + str([n['count'] for n in candidate]))
            try:
                skipped = _skip_added(observed, blobs, candidate_sources, base_sources)
                runtime_skip = any(n.get('skip_counts') is not None and b.get('skip_counts') is not None
                                   and any(n['skip_counts'][key] > b['skip_counts'][key] for key in n['skip_counts'])
                                   for n, b in zip(candidate, counts))
                check('skip_added', 'fail' if skipped or runtime_skip else 'pass',
                      str(skipped) if skipped else 'runtime_skips_increased' if runtime_skip else 'no_added_markers')
            except (SyntaxError, ValueError, tokenize.TokenError, RecursionError):
                check('skip_added', 'not_seen', 'source_parse_unavailable')
            try:
                missing = _assertion_missing(observed, blobs, candidate_sources)
                check('assertion_missing', 'fail' if missing else 'pass', str(missing) if missing else 'assertions_present')
            except (SyntaxError, ValueError, tokenize.TokenError, RecursionError):
                check('assertion_missing', 'not_seen', 'source_parse_unavailable')
            check('tests_missing', 'fail' if not tests and any(not is_doc(p) for p in paths) else 'pass',
                  'changed_tests=' + str(len(tests)))
            # Never reuse a base tree polluted by its test run (imports/cache/files).
            red_root = Path(overlay).resolve()
            red = gate_lite._checkout(repo_path, worktree.base_commit, red_root, {}, deadline)
            _, final_files, fingerprints = gate_lite._collect(
                Path(worktree.path), worktree.base_commit, None, deadline, set(), observed['tree'])
            final_oids = {fp[0][1:].split()[3] for fp in fingerprints if set(fp[0][1:].split()[3]) != {'0'}}
            final_blobs = gate_lite._blobs(Path(worktree.path), final_oids, deadline)
            overlaid = False
            for file, fingerprint in zip(final_files, fingerprints):
                if not is_test(file['path']):
                    continue
                # Overlay changed/added tests only; retain the rest of the base.
                if file['new_mode'] == '000000':
                    continue
                target = red / file['path']
                if not gate_lite._inside_copy(red, file['path'], {red / 'node_modules'}):
                    raise ValueError('unsafe_test_overlay')
                safe_write_bytes(target, final_blobs[fingerprint[0][1:].split()[3]],
                                 mode=0o755 if file['new_mode'] == '100755' else 0o644)
                overlaid = True
            red_results = (_tests(selected, red, directory / 'red', command_timeout_s, deadline, red_root, interpreter)
                           if overlaid and selected else [])
            failed = any(v['exit_code'] is not None and v['exit_code'] > 0 for v in red_results)
            unknown = not selected or any(v['exit_code'] is None for v in red_results)
            if docs_only(paths):
                # T54c: a docs-only diff has no behaviour to prove red; other checks still run.
                check('red_first_missing', 'pass', 'docs_only')
            else:
                check('red_first_missing', 'pass' if failed else 'not_seen' if unknown else 'fail',
                      'tests_fail_on_base' if failed else 'no_observed_failure_on_base')
    except Exception:
        completed = {v['id'] for v in checks}
        for identifier in CHECK_IDS:
            if identifier not in completed:
                check(identifier, 'not_seen', 'observation_unavailable')
        if len(checks) == len(completed):
            check('observation', 'not_seen', 'observation_unavailable')
    receipt = dict(schema='orcexe.task.l0.v1', tree=observed['tree'], checks=checks,
                   passed=all(v['status'] == 'pass' for v in checks))
    isolation.annotate(receipt)
    safe_write_json(directory / 'result.json', receipt)
    return receipt
