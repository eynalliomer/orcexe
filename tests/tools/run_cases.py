#!/usr/bin/env python3
"""T62 / C35: run real-world failure cases through the real `orcexe task` path with fake CLIs.

    python3 tests/tools/run_cases.py CASES_DIR [--json]

Every directory under CASES_DIR holding a ``case.json`` (schema ``orcexe.case.v1``) is one case.
For each case the runner builds a temporary Git repository from ``repo.files``, writes an approved
one-task plan (lane, allow, done, notes, routed to the fake Codex executor), and dispatches it with
``python -m orcexe task`` in a child process whose homes, temp and state directories all live in
the case's temporary directory. The fake Codex executor applies ``executor.writes`` in its
worktree on every attempt; one fake Claude wrapper answers L1 (``evaluator``) and auditor
(``auditor``) calls, told apart by the brief it is pointed at. A risky case first records the
contract proposal and its approval, exactly as a user would, then dispatches again.

What is recorded comes from the task ledger only (never from tool output): the final state and
reason, the first layer that caught the failure (``gate``, ``l0``, ``l1`` or ``audit``) with its
check id, the model calls started up to and including that catch, and whether an accepted ref or
landing command was produced.

``model_calls_before_catch`` counts the reviewer model calls (L1 and auditor) started before the
catching layer fired, including the call that caught it. The executor call that produced the
work under review is counted separately (``calls_before_catch.executor``): it is the case's
subject, not a check, so a deterministic catch (gate, L0) is one with zero reviewer calls.

Exit 0 only when every case matches its ``expect`` (never accepted, ``caught_by`` equal, model
calls before the catch at most the expected maximum); otherwise exit 1 and say which.
"""

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import tempfile
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / 'plugins/orcexe/src'
FAKE = ROOT / 'tests/fixtures/fake-cli'
for entry in (str(SOURCE), str(ROOT)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from orcexe import classify, planning  # noqa: E402
from orcexe.task_l0 import CHECK_IDS as L0_CHECK_IDS  # noqa: E402
from tests.unit._fixtures import git_environment, non_repository_root  # noqa: E402
from tests.unit.test_task import write_approved  # noqa: E402


SCHEMA = 'orcexe.case.v1'
LANES = ('standard', 'risky')
LAYERS = ('gate', 'l0', 'l1', 'audit')
ROUTE = dict(tool='codex', model='gpt-6.1-sol', effort='high')
RISKY_BASIS = ['manager:risky', 'cli:classify:risky']
AUDIT_SCHEMA = 'orcexe.audit.v1'
# Task-level rejections the auditor stage causes (everything else a gate.failed names is the gate's).
AUDIT_REASONS = {'audit', 'auditor_wrote'}
CASE_TIMEOUT_S = 900


class CaseError(ValueError):
    pass


# ---------------------------------------------------------------- case format

def _relative(path, where):
    if not isinstance(path, str) or not path:
        raise CaseError(where + ': path must be a non-empty string')
    pure = PurePosixPath(path)
    if pure.is_absolute() or '..' in pure.parts or '\\' in path or pure.parts[:1] == ('.git',):
        raise CaseError(where + ': path must be repo-relative: ' + path)
    return path


def load_case(directory):
    """Parse and validate ``case.json``; fill the safe (passing) defaults."""
    directory = Path(directory)
    try:
        value = json.loads((directory / 'case.json').read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise CaseError('case.json unreadable: ' + str(error))
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise CaseError('schema must be ' + SCHEMA)
    for key in ('id', 'title'):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise CaseError(key + ' must be a non-empty string')
    if value.get('lane') not in LANES:
        raise CaseError('lane must be one of ' + ', '.join(LANES))
    files = (value.get('repo') or {}).get('files')
    if not isinstance(files, dict) or not files:
        raise CaseError('repo.files must be a non-empty object')
    for path, text in files.items():
        _relative(path, 'repo.files')
        if not isinstance(text, str):
            raise CaseError('repo.files: content must be a string: ' + path)
    task = value.get('task')
    if not isinstance(task, dict) or not isinstance(task.get('goal'), str) or not task['goal'].strip():
        raise CaseError('task.goal must be a non-empty string')
    for key in ('allow', 'done'):
        if (not isinstance(task.get(key), list) or not task[key]
                or not all(isinstance(x, str) and x.strip() for x in task[key])):
            raise CaseError('task.' + key + ' must be a non-empty list of strings')
    if not isinstance(task.get('notes', ''), str):
        raise CaseError('task.notes must be a string')
    executor = dict(scenario='success', writes=[], final_text=None)
    executor.update(value.get('executor') or {})
    if not isinstance(executor['scenario'], str):
        raise CaseError('executor.scenario must be a string')
    if not isinstance(executor['writes'], list):
        raise CaseError('executor.writes must be a list')
    for write in executor['writes']:
        if not isinstance(write, dict) or 'path' not in write:
            raise CaseError('executor.writes: each entry needs a path')
        _relative(write['path'], 'executor.writes')
        if write.get('delete') is True:
            if set(write) - {'path', 'delete'}:
                raise CaseError('executor.writes: a delete entry has only path and delete')
        elif not isinstance(write.get('content'), str) or set(write) - {'path', 'content'}:
            raise CaseError('executor.writes: entry needs string content (or delete: true)')
    if executor['final_text'] is not None and not isinstance(executor['final_text'], str):
        raise CaseError('executor.final_text must be a string')
    rubric = (value.get('evaluator') or {}).get('rubric')
    if rubric is None:  # safe default: every done criterion passes
        rubric = [dict(id='d' + str(i), **{'pass': True}, reason='observed behavior')
                  for i in range(1, len(task['done']) + 1)]
    if not (rubric in ('invalid', 'unavailable') or (isinstance(rubric, list) and rubric and all(
            isinstance(c, dict) and set(c) == {'id', 'pass', 'reason'} for c in rubric))):
        raise CaseError('evaluator.rubric must be a criteria list, "invalid" or "unavailable"')
    auditor = value.get('auditor')
    if auditor is None:  # safe default: a passing audit with no findings
        auditor = dict(schema=AUDIT_SCHEMA, verdict='pass', findings=[])
    if not (auditor in ('invalid', 'unavailable') or isinstance(auditor, dict)):
        raise CaseError('auditor must be an audit object, "invalid" or "unavailable"')
    expect = value.get('expect')
    if (not isinstance(expect, dict) or expect.get('accepted') is not False
            or expect.get('caught_by') not in LAYERS
            or type(expect.get('max_model_calls_before_catch')) is not int
            or expect['max_model_calls_before_catch'] < 0):
        raise CaseError('expect needs accepted: false, caught_by in ' + '|'.join(LAYERS)
                        + ' and a non-negative integer max_model_calls_before_catch')
    return dict(id=value['id'], title=value['title'], lane=value['lane'], files=dict(files),
                task=dict(goal=task['goal'], allow=list(task['allow']), done=list(task['done']),
                          notes=task.get('notes', '')),
                executor=executor, rubric=rubric, auditor=auditor, expect=dict(expect))


def find_cases(root):
    root = Path(root)
    if (root / 'case.json').is_file():
        return [root]
    return sorted(p.parent for p in root.glob('*/case.json'))


# ---------------------------------------------------------------- ledger -> layer mapping

def classify_records(records):
    """First catch and the model calls started up to it, from the task's ledger records in order.

    Returns ``dict(caught_by, check, calls, calls_before_catch)``; ``caught_by`` is None when no
    layer failed (accepted, or ``gate_passed`` with an unavailable/invalid reviewer).
    """
    calls = dict(executor=0, l1=0, auditor=0)
    caught = None
    for record in records:
        event, data = record.get('event'), record.get('data') or {}
        if event == 'attempt.started':
            calls['executor'] += 1
        elif event == 'l1.started':
            calls['l1'] += 1
        elif event == 'audit.started':
            calls['auditor'] += 1
        if caught is not None:
            continue
        if event == 'l0.failed':
            failed = data.get('failed_checks') or ['l0']
            caught = ('l0', failed[0])
        elif event == 'l1.verdict':
            failing = [m.rpartition(':')[0] for m in data.get('criteria') or [] if m.endswith(':fail')]
            if failing:
                caught = ('l1', failing[0])
        elif event == 'audit.verdict' and data.get('verdict') == 'fail':
            marks = data.get('findings') or ['audit']
            caught = ('audit', marks[0])
        elif event == 'gate.failed':
            reason = data.get('reason') or 'gate'
            layer = 'audit' if reason in AUDIT_REASONS else 'l0' if reason in L0_CHECK_IDS else 'gate'
            caught = (layer, reason)
        if caught is not None:
            before = dict(calls)
    if caught is None:
        return dict(caught_by=None, check=None, calls=calls, calls_before_catch=None)
    return dict(caught_by=caught[0], check=caught[1], calls=calls, calls_before_catch=before)


def reviewer_calls(calls_before_catch):
    if calls_before_catch is None:
        return None
    return calls_before_catch['l1'] + calls_before_catch['auditor']


def compare(case, observed):
    """Mismatch messages for one case (empty when it matches its expectation)."""
    expect, problems = case['expect'], []
    if observed.get('error'):
        return ['runner error: ' + observed['error']]
    if observed['accepted']:
        problems.append('accepted (state ' + str(observed['state']) + ')')
    if observed['land'] or observed['accepted_ref']:
        problems.append('a landing command or accepted ref was produced')
    if observed['caught_by'] != expect['caught_by']:
        problems.append('caught_by ' + str(observed['caught_by']) + ' != expected ' + expect['caught_by'])
    calls = observed['model_calls_before_catch']
    if calls is not None and calls > expect['max_model_calls_before_catch']:
        problems.append('model calls before catch ' + str(calls) + ' > expected max '
                        + str(expect['max_model_calls_before_catch']))
    return problems


# ---------------------------------------------------------------- fakes

EXECUTOR = '''#!{python}
import json, os, runpy, sys
from pathlib import Path
config = json.loads(Path({config!r}).read_text(encoding="utf-8"))
os.environ["ORCEXE_FAKE_SCENARIO"] = "success"
os.environ["ORCEXE_FAKE_CALLS"] = config["calls"]
if "exec" in sys.argv:
    os.environ["ORCEXE_FAKE_SCENARIO"] = config["scenario"]
    root = Path.cwd()
    for write in config["writes"]:
        path = root / write["path"]
        if write.get("delete"):
            if path.exists():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(write["content"], encoding="utf-8")
    if config["final_text"] is not None:
        Path(config["final_file"]).write_text(config["final_text"], encoding="utf-8")
        os.environ["ORCEXE_FAKE_FINAL_TEXT_FILE"] = config["final_file"]
sys.path.insert(0, {fake_dir!r})
runpy.run_path({fake!r}, run_name="__main__")
'''

REVIEWER = '''#!{python}
import json, os, runpy, sys
from pathlib import Path
config = json.loads(Path({config!r}).read_text(encoding="utf-8"))
os.environ["ORCEXE_FAKE_SCENARIO"] = "success"
os.environ["ORCEXE_FAKE_CALLS"] = config["calls"]
if "-p" in sys.argv:
    role = "l1"
    marker = "Read the task brief at "
    for token in sys.argv:
        if marker in token:
            brief = token.split(marker, 1)[1].split(" and carry it out", 1)[0]
            try:
                if {audit_schema!r} in Path(brief).read_text(encoding="utf-8"):
                    role = "audit"
            except OSError:
                pass
    answer = config[role]
    counter = Path(config["dir"]) / (role + "-count.json")
    n = json.loads(counter.read_text()) if counter.exists() else 0
    counter.write_text(json.dumps(n + 1))
    os.environ["ORCEXE_FAKE_SCENARIO"] = answer["scenario"]
    final = Path(config["dir"]) / (role + "-final.txt")
    final.write_text(answer["text"], encoding="utf-8")
    os.environ["ORCEXE_FAKE_FINAL_TEXT_FILE"] = str(final)
sys.path.insert(0, {fake_dir!r})
runpy.run_path({fake!r}, run_name="__main__")
'''


def _answer(value, rubric):
    """The reviewer's final text and fake scenario for one role."""
    if value == 'unavailable':
        return dict(text='', scenario='quota')
    if value == 'invalid':
        return dict(text='Looks fine to me.', scenario='success')
    if rubric:
        value = dict(schema='orcexe.rubric.v1', criteria=value, overall_pass=all(c['pass'] for c in value))
    return dict(text=json.dumps(value), scenario='success')


def _script(path, template, **values):
    path.write_text(template.format(python=sys.executable, fake_dir=str(FAKE), **values), encoding='utf-8')
    path.chmod(0o700)
    return str(path)


# ---------------------------------------------------------------- one case

def _environment(base):
    """A hermetic child environment: every home, cache and temp dir inside ``base``."""
    home, tmp = base / 'home', base / 'tmp'
    home.mkdir()
    tmp.mkdir()
    env = {key: os.environ[key] for key in ('SystemRoot', 'SYSTEMROOT', 'WINDIR', 'PATHEXT', 'COMSPEC')
           if key in os.environ}
    env.update(PATH=os.pathsep.join((str(Path(sys.executable).parent), os.defpath, os.environ.get('PATH', ''))),
               HOME=str(home), USERPROFILE=str(home), TMPDIR=str(tmp), TMP=str(tmp), TEMP=str(tmp),
               CODEX_HOME=str(base / 'codex-home'), CLAUDE_CONFIG_DIR=str(home / '.claude'),
               CURSOR_CONFIG_DIR=str(home / '.cursor'), ORCEXE_STATE_DIR=str(home / 'state'),
               XDG_CACHE_HOME=str(home / '.cache'), XDG_CONFIG_HOME=str(home / '.config'),
               GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TERMINAL_PROMPT='0', PYTHONDONTWRITEBYTECODE='1', PYTHONPATH=str(SOURCE))
    for name in ('ORCEXE_NO_SANDBOX', 'ORCEXE_TEST_TIME_SCALE'):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _git(git, env, directory, *args):
    return subprocess.run([git, *args], cwd=str(directory), env=env, stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
                          timeout=60).stdout.decode('utf-8')


def _orcexe(env, *args):
    completed = subprocess.run([sys.executable, '-B', '-m', 'orcexe', *args], env=env, cwd=env['HOME'],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               timeout=CASE_TIMEOUT_S)
    return completed.returncode, completed.stdout.decode('utf-8', 'replace'), completed.stderr.decode('utf-8', 'replace')


def _ledger(main):
    path = main / '.orcexe/ledger.jsonl'
    records = []
    if path.exists():
        for line in path.read_text(encoding='utf-8').splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('task') == 'T1':
                records.append(record)
    return records


def run_case(case, keep=None):
    """Run one validated case in its own temporary directory; return the observation."""
    with tempfile.TemporaryDirectory(prefix='orcexe-case-') as temporary:
        base = Path(temporary).resolve()
        non_repository_root(base)
        env = _environment(base)
        git = git_environment(env, base)
        if not git:
            return dict(error='git unavailable')
        main = base / 'main'
        main.mkdir()
        _git(git, env, main, 'init', '--quiet', '--template=')
        for key, value in (('user.name', 'Case Runner'), ('user.email', 'cases@example.invalid'),
                           ('commit.gpgsign', 'false'), ('core.hooksPath', str(base / 'no-hooks'))):
            _git(git, env, main, 'config', key, value)
        for name, text in case['files'].items():
            path = main / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding='utf-8')
        _git(git, env, main, 'add', '-A')
        _git(git, env, main, 'commit', '-qm', 'case base')
        head = _git(git, env, main, 'rev-parse', 'HEAD').strip()

        fakes = base / 'fakes'
        fakes.mkdir()
        executor = dict(case['executor'], calls=str(fakes / 'calls.jsonl'), final_file=str(fakes / 'executor-final.txt'))
        (fakes / 'executor.json').write_text(json.dumps(executor), encoding='utf-8')
        env['ORCEXE_BIN_CODEX'] = _script(fakes / 'codex', EXECUTOR, config=str(fakes / 'executor.json'),
                                          fake=str(FAKE / 'codex'))
        if case['rubric'] == 'unavailable':
            # No reviewer binary at all: no L1 (and, on risky, no auditor) can run.
            env['ORCEXE_BIN_CLAUDE_CODE'] = str(fakes / 'absent-claude')
        else:
            reviewer = dict(dir=str(fakes), calls=str(fakes / 'calls.jsonl'),
                            l1=_answer(case['rubric'], True), audit=_answer(case['auditor'], False))
            (fakes / 'reviewer.json').write_text(json.dumps(reviewer), encoding='utf-8')
            env['ORCEXE_BIN_CLAUDE_CODE'] = _script(fakes / 'claude', REVIEWER, config=str(fakes / 'reviewer.json'),
                                                    fake=str(FAKE / 'claude'), audit_schema=AUDIT_SCHEMA)
        env['ORCEXE_BIN_CURSOR'] = str(fakes / 'absent-cursor')

        task = dict(id='T1', goal=case['task']['goal'], files={'allow': case['task']['allow']},
                    done=case['task']['done'], depends_on=[], lane=case['lane'], size='S', parallel=False,
                    route=dict(ROUTE))
        if case['task']['notes']:
            task['notes'] = case['task']['notes']
        if case['lane'] == 'risky':
            task['lane_basis'] = list(RISKY_BASIS)
        plan = dict(schema='orcexe.plan.v1', head=head, classifier=classify.CLASSIFIER_VERSION, status='approved',
                    tasks=[task], goal=case['title'], decisions=[], assumptions=[])
        plan['content_sha256'] = planning.content_sha256(plan)
        plan['plan_id'] = planning.plan_id(plan['content_sha256'])
        with mock.patch.dict(os.environ, env, clear=True):
            write_approved(main, plan)

        dispatch = ('task', '--plan', plan['plan_id'], '--task', 'T1', '--repo', str(main),
                    '--timeout-min', '0.5', '--json')
        code, out, err = _orcexe(env, *dispatch)
        if case['lane'] == 'risky':
            try:
                proposed = json.loads(out)
                digest = proposed['contract']['approval_sha256']
            except (ValueError, KeyError, TypeError):
                return dict(error='risky contract was not proposed: ' + (err or out)[-400:])
            approved, _, approval_error = _orcexe(env, 'task', '--approve-contract', plan['plan_id'], 'T1',
                                                  digest, '--repo', str(main))
            if approved != 0:
                return dict(error='contract approval failed: ' + approval_error[-400:])
            code, out, err = _orcexe(env, *dispatch)
        try:
            result = json.loads(out)
        except ValueError:
            return dict(error='orcexe task printed no JSON (exit ' + str(code) + '): ' + err[-400:])
        records = _ledger(main)
        mapping = classify_records(records)
        refs = _git(git, env, main, 'for-each-ref', 'refs/orcexe/accepted/').strip()
        states = [r['data'].get('to') for r in records if r.get('event') == 'task.state']
        isolation = [r['data'].get('isolation') for r in records if r.get('event') in ('gate.failed', 'gate.passed')]
        if keep is not None:
            shutil.copytree(str(main / '.orcexe'), str(Path(keep) / case['id']), dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns('*.lock'))
        return dict(state=result.get('state'), reason=result.get('reason'), attempts=result.get('attempts'),
                    exit_code=code, accepted=result.get('state') == 'accepted' or 'accepted' in states,
                    land=result.get('land') is not None, accepted_ref=bool(refs),
                    caught_by=mapping['caught_by'], check=mapping['check'], calls=mapping['calls'],
                    calls_before_catch=mapping['calls_before_catch'],
                    model_calls_before_catch=reviewer_calls(mapping['calls_before_catch']),
                    isolation=next((i for i in isolation if i), None), error=None)


def run_all(root, keep=None):
    cases = []
    for directory in find_cases(root):
        entry = dict(dir=directory.name)
        try:
            case = load_case(directory)
        except CaseError as error:
            entry.update(id=directory.name, match=False, mismatches=['invalid case: ' + str(error)], error=str(error))
            cases.append(entry)
            continue
        try:
            observed = run_case(case, keep)
        except Exception as error:  # report the case, keep running the others
            observed = dict(error=type(error).__name__ + ': ' + str(error))
        problems = compare(case, observed)
        entry.update(id=case['id'], title=case['title'], lane=case['lane'], expect=case['expect'], **observed)
        entry.update(match=not problems, mismatches=problems)
        cases.append(entry)
    summary = dict(total=len(cases), accepted=sum(1 for c in cases if c.get('accepted')),
                   mismatches=sum(1 for c in cases if not c['match']))
    return dict(cases=cases, summary=summary)


def render(report):
    header = ('case', 'lane', 'state / reason', 'caught_by: check', 'calls e/l1/aud', 'before catch', 'land', 'expect', 'ok')
    rows = []
    for c in report['cases']:
        calls = c.get('calls')
        before = c.get('calls_before_catch')
        expect = c.get('expect') or {}
        rows.append((
            c['id'], c.get('lane') or '-',
            '{} / {}'.format(c.get('state'), c.get('reason')) if c.get('state') else '-',
            '{}: {}'.format(c.get('caught_by'), c.get('check')) if c.get('caught_by') else 'none',
            '{}/{}/{}'.format(calls['executor'], calls['l1'], calls['auditor']) if calls else '-',
            '{} (exec {})'.format(c.get('model_calls_before_catch'), before['executor']) if before else '-',
            'yes' if c.get('land') or c.get('accepted_ref') else 'no',
            '{} <={}'.format(expect.get('caught_by'), expect.get('max_model_calls_before_catch')) if expect else '-',
            'ok' if c['match'] else 'MISMATCH'))
    widths = [max(len(str(r[i])) for r in rows + [header]) for i in range(len(header))]
    line = lambda r: '| ' + ' | '.join(str(v).ljust(w) for v, w in zip(r, widths)) + ' |'
    out = [line(header), '|' + '|'.join('-' * (w + 2) for w in widths) + '|'] + [line(r) for r in rows]
    for c in report['cases']:
        for problem in c['mismatches']:
            out.append('MISMATCH ' + c['id'] + ': ' + problem)
    s = report['summary']
    out.append('cases: {total}, accepted: {accepted}, mismatches: {mismatches}'.format(**s))
    return '\n'.join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('cases_dir')
    parser.add_argument('--json', action='store_true', help='emit {"cases": [...], "summary": {...}}')
    parser.add_argument('--keep', metavar='DIR', help='copy each case\'s .orcexe/ (ledger, receipts) to DIR/<id>')
    options = parser.parse_args(argv)
    if not find_cases(options.cases_dir):
        print('no case.json found under ' + options.cases_dir, file=sys.stderr)
        return 1
    report = run_all(options.cases_dir, options.keep)
    print(json.dumps(report, indent=2, sort_keys=True) if options.json else render(report))
    return 0 if report['summary']['mismatches'] == 0 and report['summary']['total'] else 1


if __name__ == '__main__':
    sys.exit(main())
