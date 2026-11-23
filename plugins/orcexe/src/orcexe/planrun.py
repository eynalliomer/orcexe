"""Sequential approved-plan execution and ledger-derived, model-free status."""

import json
import os
from pathlib import Path
import shlex
import signal
import sys
import threading

from orcexe import do, fsio, gitutil, intake, isolation, ledger, redact, runguard


FINAL = frozenset(('accepted', 'exhausted', 'rejected', 'gate_passed', 'blocked', 'diagnosing'))
UNFINISHED = frozenset(('interrupted', 'running', 'contract.frozen'))
# T60 (T57 §3.6): parked, not final: a risky task waiting for the user's contract approval.
PARKED = frozenset(('awaiting_approval',))
EXIT_REASONS = {'completed': 0, 'partial': 1, 'interrupted': 130,
                'dependency_failed': 1, 'lane_not_supported': 1,
                'run_interrupted': 2, 'task_running': 2, 'internal_error': 70,
                'ledger_corrupt': 2, 'ledger_head_mismatch': 2}


def task_records(plan, log):
    """Project CLI records only, scoped to this plan and latest frozen contract."""
    identifier = plan['plan_id']
    result = {t['id']: dict(task_id=t['id'], state='pending', reason=None,
                           attempts=0, contract=None, l1_basis=None, l1_pass=None,
                           proposal=None, audit_pass=None, auditor=None) for t in plan['tasks']}
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
                        l1_basis=None, l1_pass=None, proposal=None, auditor=None)
        elif event == 'contract.proposed':
            item['proposal'] = data['approval_sha256']
        elif event == 'attempt.started':
            item.update(state='running', reason=None, attempts=data['attempt'], l1_pass=None, audit_pass=None)
        elif event == 'audit.started':
            item['audit_pass'] = dict(model=data['model'], same_model_as_l1=data.get('same_model_as_l1') is True)
        elif event == 'audit.verdict':
            if data.get('verdict') != 'pass':
                item['audit_pass'] = None
        elif event == 'l1.verdict':
            # The passing verdict (asked or reused) of the attempt that is accepted next.
            passed = (data.get('reason') is None and data['criteria']
                      and all(c.endswith(':pass') for c in data['criteria']))
            item['l1_pass'] = (data.get('basis') if data.get('basis') in L1_BASES[:3] else 'unknown') if passed else None
        elif event == 'task.state':
            item.update(state=data['to'], reason=data['reason'],
                        l1_basis=(item['l1_pass'] or 'none') if data['to'] == 'accepted' else None,
                        auditor=item['audit_pass'] if data['to'] == 'accepted' else None)
    # T59: the latest decision, not the ledger, says which tasks are deferred.
    for key, entry in deferred_tasks(plan).items():
        if key in result and result[key]['state'] == 'pending':
            result[key].update(state='deferred', reason=entry.get('reason'))
    for key, item in result.items():
        if item['state'] == 'deferred' and key not in deferred_tasks(plan):
            item.update(state='pending', reason=None)
    return result


def deferred_tasks(plan):
    """{id: entry} of the latest decision's deferred list (empty for an unapproved plan)."""
    approval = plan.get('approval')
    if plan.get('status') != 'approved' or not isinstance(approval, dict) or not isinstance(approval.get('deferred'), list):
        return {}
    return {d['id']: d for d in approval['deferred'] if isinstance(d, dict) and isinstance(d.get('id'), str)}


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
    form = dict(ref=ref, command='git -C ' + shlex.quote(str(main)) + ' ' + action + shlex.quote(ref),
                l1_basis=l1_basis(plan, task_records(plan, log)))
    records = task_records(plan, log)
    same = [key + ' (' + item['auditor']['model'] + ')' for key, item in records.items()
            if item['state'] == 'accepted' and item['auditor'] and item['auditor']['same_model_as_l1']]
    if same:
        # Decision 027 Ş3: no other eligible cross-family model, so the auditor was the L1 model.
        form['same_model_as_l1'] = same
    parked = deferred_tasks(plan)
    deferred = [t['id'] for t in plan['tasks'] if t['id'] in parked]
    if deferred:
        # T59 / decision 027 Ş4: landing moves HEAD, so these can never be opened for this plan.
        form['deferred'] = deferred
    return form


def public_tasks(records):
    return [{k: item[k] for k in ('task_id', 'state', 'reason', 'attempts', 'l1_basis')}
            for item in records.values()]


def run_state(records):
    states = [item['state'] for item in records.values()]
    if any(s in UNFINISHED for s in states):
        return 'interrupted'
    return 'completed' if all(s == 'accepted' for s in states) else 'partial'


def contract_approvals(plan, records):
    """T57 §3.2 item 4: the exact command the user types for each parked risky task."""
    from orcexe import risky
    return [dict(task_id=key, approval_sha256=item['proposal'],
                 command=risky.approve_command(plan['plan_id'], key, item['proposal']))
            for key, item in records.items() if item['state'] in PARKED and item['proposal']]


def status(main, plan, log):
    from orcexe import planning
    records = task_records(plan, log)
    # P3-2: the ledger's approval.revoked wins over a plan file that still says approved.
    revoked = plan.get('status') == 'approved' and planning.revoked(plan['plan_id'], log)
    result = dict(schema='orcexe.status.v1', plan_id=plan['plan_id'],
                  plan_status='revoked' if revoked else plan['status'], tasks=public_tasks(records),
                  land=land(main, plan, log))
    approvals = contract_approvals(plan, records)
    if approvals:
        result['contract_approvals'] = approvals
    return result


def _approval_lines(result):
    lines = []
    for item in result.get('contract_approvals') or []:
        lines.append('Awaiting your contract approval (' + item['task_id'] + '; only you run this, never a host '
                     'agent): ' + item['command'])
    return lines


def render(plan, result):
    lines = ['# Plan ' + plan['plan_id'], '', 'Status: ' + result['plan_status'], '',
             '| ID | Lane | State | Attempts | Reason | L1 basis |', '| --- | --- | --- | --- | --- | --- |']
    lanes = {t['id']: t['lane'] for t in plan['tasks']}
    for item in result['tasks']:
        values = (item['task_id'], lanes[item['task_id']], item['state'], item['attempts'], item['reason'] or '-',
                  item.get('l1_basis') or '-')
        lines.append('| ' + ' | '.join(str(v).replace('|', '&#124;').replace('\n', ' ') for v in values) + ' |')
    approvals = _approval_lines(result)
    if approvals:
        lines += [''] + approvals
    if result['land']:
        lines += ['', 'Human landing command (review the integration chain):', '', result['land']['command']]
        lines += _basis_lines(result['land'])
    return '\n'.join(lines) + '\n'


def _basis_lines(land_form):
    basis = (land_form or {}).get('l1_basis')
    lines = []
    if basis and basis['tasks']:
        lines += ['', 'L1 basis behind this ref (weakest: ' + str(basis['weakest']) + '): '
                  + ', '.join(t['task_id'] + '=' + str(t['basis']) for t in basis['tasks'])]
    same = (land_form or {}).get('same_model_as_l1')
    if same:
        lines += ['', 'Auditor same model as L1: same_model_as_l1 (' + ', '.join(same)
                  + '; no other eligible cross-family model)']
    deferred = (land_form or {}).get('deferred')
    if deferred:
        lines += ['', deferred_warning(deferred)]
    return lines


def deferred_warning(deferred):
    names = '/'.join(deferred)
    return ('Warning: landing closes deferred ' + names + ' for this plan for good (HEAD moves, so '
            '`--open` refuses with plan_stale); open them before landing, or run a new `orcexe plan --goal`.')


def write_status(main, plan, log):
    path = main / '.orcexe/status.md'
    from orcexe import current_state, published
    published.report(published.write_text(path.parent, path.name, render(plan, redact.redact_obj(status(main, plan, log)))))
    published.report(current_state.write(main, plan=plan, log=log))


def _main(repo):
    explicit_repo = repo is not None
    try:
        repo_path = gitutil.resolve_repo_path(repo) if explicit_repo else Path.cwd().resolve()
        if not gitutil.is_repo(repo_path):
            raise ValueError('not_a_repository')
        main = Path(gitutil.main_worktree(repo_path))
    except gitutil.GitError as error:
        raise ValueError('not_a_repository') from error
    try:
        fsio.require_main_orcexe(main)
    except fsio.OrcexeDirError as error:
        raise ValueError('orcexe_invalid') from error
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


@ledger.in_session
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
        # T55: the whole chain and its external head ref before any state is read
        # (an earlier terminal tamper marker is reported first).
        runguard.check_marker(main)
        log.verify_head()
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
            if records[key]['state'] in FINAL or key not in plan['approval']['approved']:
                continue  # T59: a deferred task is never dispatched; the run ends partial.
            states = [records[dep]['state'] for dep in item['depends_on']]
            if any(s in FINAL and s != 'accepted' for s in states):
                reason = 'dependency_failed'
            elif any(s in PARKED or s == 'pending' for s in states):
                continue  # T60: behind a contract awaiting approval: stays pending, never blocked.
            elif any(s != 'accepted' for s in states):
                reason = 'dependency_failed'
            else:
                reason = None
            if reason:
                log.append('task.state', key, dict(plan_id=identifier, **{'from': records[key]['state'],
                           'to': 'blocked'}, reason=reason))
            else:
                outcome = task.run_task(identifier, key, main, _context=plan, _resume=resume)
                if outcome['reason'] == 'state_tampered':
                    # P2-2: never read (or dispatch dependants from) a ledger written during a run.
                    raise isolation.StateTampered()
                # Pre-dispatch refusals do not freeze contracts, but must be final
                # ledger states for a plan run, so they cannot silently re-run.
                if task_records(plan, log)[key]['state'] == 'pending':
                    log.append('task.state', key, dict(plan_id=identifier, **{'from': 'pending',
                               'to': outcome['state']}, reason=outcome['reason'] or outcome['state']))
                if outcome['exit_code'] == 130:
                    raise do._Cancelled()
                # P3-1 (§4): a decision forged during that task's executor window stops the run
                # here (and revokes the approval), before any later task is dispatched.
                task._read_plan(main, identifier, None, log)
            write_status(main, plan, log)
        records = task_records(plan, log)
        result.update(state=run_state(records), tasks=public_tasks(records), land=land(main, plan, log))
        approvals = contract_approvals(plan, records)
        if approvals:
            result['contract_approvals'] = approvals
        write_status(main, plan, log)
        return result, 0 if result['state'] == 'completed' else 1
    except (KeyboardInterrupt, do._Cancelled):
        if plan is not None:
            result.update(tasks=public_tasks(task_records(plan, log)), land=land(main, plan, log))
            write_status(main, plan, log)
        result['state'] = 'interrupted'
        return result, 130
    except (ValueError, ledger.LedgerError) as error:
        reason = str(error) if isinstance(error, ValueError) else ledger.reason(error)
        result['reason'] = reason
        if isinstance(getattr(error, 'detail', None), str):
            result['detail'] = error.detail
        return result, 2
    except Exception:
        result['reason'] = 'internal_error'
        return result, 70
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        _release(lock_path, lock_info)


def _print(result, code, json_output):
    detail = result.pop('detail', None)
    clean = redact.redact_obj(result)
    if json_output:
        print(json.dumps(clean, ensure_ascii=False))
    else:
        print('Plan: ' + str(clean['plan_id']))
        print('Status: ' + clean.get('state', clean.get('plan_status', 'unknown')))
        from orcexe.risky import one_line
        for item in clean['tasks']:
            print(one_line('{}: {} ({} attempts) {}'.format(item['task_id'], item['state'], item['attempts'],
                                                            item['reason'] or '')))
        for line in _approval_lines(clean):
            print(one_line(line))
        if clean['land']:
            print('Human landing command:\n' + clean['land']['command'])
            for line in _basis_lines(clean['land'])[1:]:
                print(line)
    if clean.get('reason'):
        print('not_seen: ' + clean['reason'], file=sys.stderr)
    if detail:
        print('detail: ' + detail, file=sys.stderr)
    if clean.get('reason') in ('ledger_corrupt', 'ledger_head_mismatch'):
        print(ledger.REPAIR_HINT, file=sys.stderr)
    elif clean.get('reason') == 'state_tampered':
        from orcexe import planning
        print(planning.UNANCHORED_HINT if detail else ledger.TAMPER_HINT, file=sys.stderr)
    elif detail == 'approval_revoked':
        from orcexe import planning
        print(planning.REVOKED_HINT, file=sys.stderr)
    return code


def run_command(args):
    result, code = run_plan(args.run, args.repo, resume=args.resume)
    return _print(result, code, args.json)


@ledger.in_session
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
        if os.path.lexists(main / runguard.MARKER):
            # T55: never report state recorded around a terminal tamper as fact.
            raise ValueError('state_tampered')
        log = ledger.Ledger(ledger.default_path(main))
        # T55: status decides nothing from a ledger whose tail is not the anchored head.
        log.verify_head()
        result = status(main, plan, log)
        return _print(result, 0, args.json)
    except (ValueError, OSError, KeyError, ledger.LedgerError) as error:
        reason = (str(error) if isinstance(error, ValueError) else
                  'ledger_head_mismatch' if isinstance(error, ledger.HeadMismatch) else
                  'ledger_corrupt' if isinstance(error, ledger.LedgerError) else 'state_corrupt')
        print('not_seen: ' + reason, file=sys.stderr)
        if reason in ('ledger_corrupt', 'ledger_head_mismatch'):
            print(ledger.REPAIR_HINT, file=sys.stderr)
        elif reason == 'state_tampered':
            print(ledger.TAMPER_HINT, file=sys.stderr)
        return 2
