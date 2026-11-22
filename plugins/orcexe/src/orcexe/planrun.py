"""Sequential approved-plan execution and ledger-derived, model-free status."""

import json
import os
from pathlib import Path
import shlex
import signal
import sys
import threading

from orcexe import do, fsio, gitutil, intake, ledger, redact


FINAL = frozenset(('accepted', 'exhausted', 'rejected', 'gate_passed', 'blocked'))
UNFINISHED = frozenset(('interrupted', 'running', 'contract.frozen'))
EXIT_REASONS = {'completed': 0, 'partial': 1, 'interrupted': 130,
                'dependency_failed': 1, 'lane_not_supported': 1,
                'run_interrupted': 2, 'task_running': 2, 'internal_error': 70}


def task_records(plan, log):
    """Project CLI records only, scoped to this plan and latest frozen contract."""
    identifier = plan['plan_id']
    result = {t['id']: dict(task_id=t['id'], state='pending', reason=None,
                           attempts=0, contract=None, l1_basis=None, l1_pass=None) for t in plan['tasks']}
    owners = {}
    for record in log.records():
        key, event, data = record['task'], record['event'], record['data']
        if event == 'contract.frozen':
            owners[key] = data['plan_id']
        if key not in result or data.get('plan_id', owners.get(key, identifier)) != identifier:
            continue
        item = result[key]
        if event == 'contract.frozen':
            item.update(state='contract.frozen', reason=None, attempts=0, contract=data,
                        l1_basis=None, l1_pass=None)
        elif event == 'attempt.started':
            item.update(state='running', reason=None, attempts=data['attempt'], l1_pass=None)
        elif event == 'l1.verdict':
            # The passing verdict (asked or reused) of the attempt that is accepted next.
            passed = (data.get('reason') is None and data['criteria']
                      and all(c.endswith(':pass') for c in data['criteria']))
            item['l1_pass'] = (data.get('basis') if data.get('basis') in L1_BASES[:3] else 'unknown') if passed else None
        elif event == 'task.state':
            item.update(state=data['to'], reason=data['reason'],
                        l1_basis=(item['l1_pass'] or 'none') if data['to'] == 'accepted' else None)
    return result


# Strongest first. 'unknown': a passing verdict recorded before T51 (no basis);
# 'none': accepted without any L1 verdict (fast lane). Neither is ever cross_family.
L1_BASES = ('cross_family', 'cursor_read_only', 'same_family', 'unknown', 'none')


def l1_basis(plan, records, extra=None):
    """Per accepted task basis behind the chain, and the weakest of them."""
    tasks = [dict(task_id=t['id'], basis=records[t['id']]['l1_basis']) for t in plan['tasks']
             if records[t['id']]['state'] == 'accepted']
    if extra is not None:
        tasks.append(dict(task_id=extra[0], basis=extra[1] if extra[1] in L1_BASES else 'none'))
    weakest = max((t['basis'] for t in tasks), key=L1_BASES.index, default=None)
    return dict(tasks=tasks, weakest=weakest)


def chain_ref(plan):
    return 'refs/orcexe/plans/' + plan['plan_id']


def chain_tip(main, plan):
    _, output = do._git(main, 'for-each-ref', '--format=%(refname) %(objectname) %(symref)', chain_ref(plan))
    for line in output.decode().splitlines():
        ref, tip, symbolic = line.split(' ', 2)
        if ref == chain_ref(plan):
            if symbolic:
                raise ValueError('state_corrupt')
            return tip
    return None


def ensure_chain(main, plan, log):
    """Create only under the dispatch lock; bind the tip to CLI acceptance records."""
    expected = plan['head']
    for record in log.records(event='task.state'):
        data = record['data']
        if data.get('plan_id') == plan['plan_id'] and data['to'] == 'accepted' and data.get('commit'):
            expected = data['commit']
    tip = chain_tip(main, plan)
    if tip is None:
        if expected != plan['head']:
            raise ValueError('state_corrupt')
        do._git(main, 'update-ref', chain_ref(plan), expected, '0' * len(expected))
    elif tip != expected:
        raise ValueError('state_corrupt')
    return expected


def advance(main, plan, commit, base):
    # Compare-and-swap refuses interference without moving HEAD or any branch.
    do._git(main, 'update-ref', chain_ref(plan), commit, base)


def land(main, plan, log):
    tip = chain_tip(main, plan)
    if tip is None or tip == plan['head']:
        return None
    action = 'merge --ff-only ' if gitutil.head(main) == plan['head'] else 'cherry-pick '
    ref = chain_ref(plan)
    return dict(ref=ref, command='git -C ' + shlex.quote(str(main)) + ' ' + action + shlex.quote(ref),
                l1_basis=l1_basis(plan, task_records(plan, log)))


def public_tasks(records):
    return [{k: item[k] for k in ('task_id', 'state', 'reason', 'attempts', 'l1_basis')}
            for item in records.values()]


def run_state(records):
    states = [item['state'] for item in records.values()]
    if any(s in UNFINISHED for s in states):
        return 'interrupted'
    return 'completed' if all(s == 'accepted' for s in states) else 'partial'


def status(main, plan, log):
    records = task_records(plan, log)
    return dict(schema='orcexe.status.v1', plan_id=plan['plan_id'],
                plan_status=plan['status'], tasks=public_tasks(records), land=land(main, plan, log))


def render(plan, result):
    lines = ['# Plan ' + plan['plan_id'], '', 'Status: ' + result['plan_status'], '',
             '| ID | Lane | State | Attempts | Reason | L1 basis |', '| --- | --- | --- | --- | --- | --- |']
    lanes = {t['id']: t['lane'] for t in plan['tasks']}
    for item in result['tasks']:
        values = (item['task_id'], lanes[item['task_id']], item['state'], item['attempts'], item['reason'] or '-',
                  item.get('l1_basis') or '-')
        lines.append('| ' + ' | '.join(str(v).replace('|', '&#124;').replace('\n', ' ') for v in values) + ' |')
    if result['land']:
        lines += ['', 'Human landing command (review the integration chain):', '', result['land']['command']]
        lines += _basis_lines(result['land'])
    return '\n'.join(lines) + '\n'


def _basis_lines(land_form):
    basis = (land_form or {}).get('l1_basis')
    if not basis or not basis['tasks']:
        return []
    return ['', 'L1 basis behind this ref (weakest: ' + str(basis['weakest']) + '): '
            + ', '.join(t['task_id'] + '=' + str(t['basis']) for t in basis['tasks'])]


def write_status(main, plan, log):
    path = main / '.orcexe/status.md'
    from orcexe import current_state, published
    published.report(published.write_text(path.parent, path.name, render(plan, redact.redact_obj(status(main, plan, log)))))
    published.report(current_state.write(main, plan=plan, log=log))


def _main(repo):
    repo = Path.cwd() if repo is None else Path(repo).resolve()
    if not gitutil.is_repo(repo):
        raise ValueError('not_a_repository')
    main = Path(gitutil.main_worktree(repo))
    if (main / '.orcexe').is_symlink():
        raise ValueError('output_symlink')
    return main


def _release(path, info):
    if info is not None:
        try:
            current = path.stat()
            if (current.st_dev, current.st_ino) == (info.st_dev, info.st_ino):
                path.unlink()
        except FileNotFoundError:
            pass


def _order(plan):
    remaining, ordered = list(plan['tasks']), []
    seen = set()
    while remaining:
        ready = next(t for t in remaining if set(t['depends_on']) <= seen)
        ordered.append(ready)
        seen.add(ready['id'])
        remaining.remove(ready)
    return ordered


def run_plan(identifier, repo=None, *, resume=False):
    from orcexe import task
    result = dict(schema='orcexe.planrun.v1', plan_id=identifier, state='partial', tasks=[], land=None)
    main = plan = log = lock_path = lock_info = None
    handlers = {}

    def interrupted(signum, frame):
        raise do._Cancelled()

    try:
        main = _main(repo)
        lock_path = main / '.orcexe/do.lock'
        lock_info = do._acquire(lock_path, identifier, lambda data: None)
        if lock_info is None:
            raise ValueError('task_running')
        log = ledger.Ledger(ledger.default_path(main))
        # The same approval/hash/HEAD/tamper checks as task, once under the lock.
        plan = task._read_plan(main, identifier, None, log)
        records = task_records(plan, log)
        result.update(state=run_state(records), tasks=public_tasks(records), land=land(main, plan, log))
        if any(t['state'] in UNFINISHED for t in records.values()) and not resume:
            raise ValueError('run_interrupted')
        ensure_chain(main, plan, log)
        if os.name == 'posix' and threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                handlers[sig] = signal.signal(sig, interrupted)
        for item in _order(plan):
            records = task_records(plan, log)
            result['tasks'] = public_tasks(records)
            key = item['id']
            if records[key]['state'] in FINAL:
                continue
            if any(records[dep]['state'] != 'accepted' for dep in item['depends_on']):
                reason = 'dependency_failed'
            elif item['lane'] == 'risky':
                reason = 'lane_not_supported'
            else:
                reason = None
            if reason:
                log.append('task.state', key, dict(plan_id=identifier, **{'from': records[key]['state'],
                           'to': 'blocked'}, reason=reason))
            else:
                outcome = task.run_task(identifier, key, main, _context=plan, _resume=resume)
                # Pre-dispatch refusals do not freeze contracts, but must be final
                # ledger states for a plan run, so they cannot silently re-run.
                if task_records(plan, log)[key]['state'] == 'pending':
                    log.append('task.state', key, dict(plan_id=identifier, **{'from': 'pending',
                               'to': outcome['state']}, reason=outcome['reason'] or outcome['state']))
                if outcome['exit_code'] == 130:
                    raise do._Cancelled()
            write_status(main, plan, log)
        records = task_records(plan, log)
        result.update(state=run_state(records), tasks=public_tasks(records), land=land(main, plan, log))
        write_status(main, plan, log)
        return result, 0 if result['state'] == 'completed' else 1
    except (KeyboardInterrupt, do._Cancelled):
        if plan is not None:
            result.update(tasks=public_tasks(task_records(plan, log)), land=land(main, plan, log))
            write_status(main, plan, log)
        result['state'] = 'interrupted'
        return result, 130
    except (ValueError, ledger.LedgerError) as error:
        reason = str(error) if isinstance(error, ValueError) else 'state_corrupt'
        result['reason'] = reason
        return result, 2
    except Exception:
        result['reason'] = 'internal_error'
        return result, 70
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        _release(lock_path, lock_info)


def _print(result, code, json_output):
    clean = redact.redact_obj(result)
    if json_output:
        print(json.dumps(clean, ensure_ascii=False))
    else:
        print('Plan: ' + str(clean['plan_id']))
        print('Status: ' + clean.get('state', clean.get('plan_status', 'unknown')))
        for item in clean['tasks']:
            print('{}: {} ({} attempts) {}'.format(item['task_id'], item['state'], item['attempts'], item['reason'] or ''))
        if clean['land']:
            print('Human landing command:\n' + clean['land']['command'])
            for line in _basis_lines(clean['land'])[1:]:
                print(line)
    if clean.get('reason'):
        print('not_seen: ' + clean['reason'], file=sys.stderr)
    return code


def run_command(args):
    result, code = run_plan(args.run, args.repo, resume=args.resume)
    return _print(result, code, args.json)


def status_command(args):
    from orcexe import task
    try:
        main = _main(args.repo)
        path = task._plan_path(main / '.orcexe')
        try:
            plan = intake._json(path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            raise ValueError('plan_missing') from None
        if not isinstance(plan, dict) or plan.get('schema') != 'orcexe.plan.v1':
            raise ValueError('plan_tampered')
        result = status(main, plan, ledger.Ledger(ledger.default_path(main)))
        return _print(result, 0, args.json)
    except (ValueError, OSError, KeyError, ledger.LedgerError) as error:
        reason = str(error) if isinstance(error, ValueError) else 'state_corrupt'
        print('not_seen: ' + reason, file=sys.stderr)
        return 2
