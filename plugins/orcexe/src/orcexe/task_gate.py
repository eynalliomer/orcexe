"""Standard-lane policy over gate_lite's raw snapshots and isolated checks.

The fast gate's one-file/comment-only policy remains unchanged. Standard tasks
may change code and add/remove plain files; both lanes inspect all historical,
staged and working changes before checking the frozen allow list.
"""

import os
from pathlib import Path, PurePosixPath
import shutil
import tempfile
import time

from orcexe import gate_lite, globs, isolation
from orcexe.adapters.base import safe_write_json, validate_run_result
from orcexe.redact import redact_text


def observe(main, wt, allow):
    """Return immutable candidate tree, complete change union and scope misses."""
    deadline = time.monotonic() + 1800
    path, base = Path(wt.path), wt.base_commit
    if not gate_lite._registered_gitfile(main, path, deadline):
        raise ValueError('git_controls_changed')
    code, symbolic = gate_lite._git(path, 'symbolic-ref', '-q', 'HEAD',
                                    allow_failure=True, deadline=deadline)
    if code or symbolic != 'refs/heads/' + wt.branch:
        raise ValueError('head_branch_mismatch')
    _, head = gate_lite._git(path, 'rev-parse', 'HEAD^{commit}', deadline=deadline)
    if head != base:
        code, _ = gate_lite._git(path, 'merge-base', '--is-ancestor', base, head,
                                allow_failure=True, deadline=deadline)
        if code:
            raise ValueError('base_not_ancestor')
    with tempfile.TemporaryDirectory(prefix='orcexe-task-snapshot-') as temporary:
        index, dirty, _, metadata, _ = gate_lite._snapshot(path, Path(temporary), deadline)
        if metadata:
            raise ValueError('nested_git_metadata')
        states = [(base, index, None), (base, None, None)]
        if head != base:
            states.append((base, None, head))
            _, history = gate_lite._git(path, 'rev-list', '--reverse', '--parents',
                                       base + '..' + head, deadline=deadline)
            for entry in history.splitlines():
                commit, *parents = entry.split()
                states.extend((parent, None, commit) for parent in parents)
        files, seen, versions = [], set(), []
        for against, observed, tree in states:
            _, changed, fingerprints = gate_lite._collect(
                path, against, observed, deadline, dirty if observed == index else set(), tree)
            for file, fingerprint in zip(changed, fingerprints):
                if fingerprint in seen:
                    continue
                seen.add(fingerprint)
                files.append(file)
                versions.append((file, fingerprint[0][1:].split()[2:4]))
        _, tree = gate_lite._git(path, 'write-tree', index=index, deadline=deadline)
    paths = sorted({f[k] for f in files for k in ('path', 'old_path') if f[k] is not None})
    outside = [p for p in paths if not any(globs.matches(p, pattern) for pattern in allow)]
    return dict(tree=tree, files=files, versions=versions, outside=outside)


@isolation.guarded
def standard_gate(*, repo_path, worktree, run_result, run_dir, controls_before,
                  commands, observed, command_timeout_s=600, total_timeout_s=1800, retry=False):
    """Run shared safety rules and project checks against immutable raw objects.

    On a retry in the same worktree the executor's own per-attempt empty_diff
    warning is not a failure: whether anything changed is decided on the
    cumulative tree against the task base (diff_collect), and every later
    check runs on that cumulative tree exactly as for the first attempt.
    """
    directory = Path(run_dir)
    result = dict(schema='orcexe.task.gate.v1', verdict='failed', checks=[],
                  tree=observed['tree'], files=observed['files'])
    deadline = time.monotonic() + total_timeout_s
    cache_dir = None

    def check(identifier, passed, detail):
        result['checks'].append(dict(id=identifier, status='pass' if passed else 'fail',
                                     detail=redact_text(detail)[:300]))
        if not passed:
            raise ValueError(identifier)

    try:
        validate_run_result(run_result)
        if type(retry) is not bool:
            raise ValueError('retry')
        check('run_status', run_result['status'] == 'completed'
              and (retry or 'empty_diff' not in run_result['warnings'])
              and 'pipes_held' not in run_result['warnings'], run_result['status'])
        check('controls', gate_lite.snapshot_controls(repo_path, worktree) == controls_before,
              'git_controls_changed')
        _, base_tree = gate_lite._git(repo_path, 'rev-parse', worktree.base_commit + '^{tree}',
                                     deadline=deadline)
        check('diff_collect', base_tree != observed['tree'], 'empty_diff')
        sources = set(gate_lite._SOURCES)
        sources.update(c.source_path for c in gate_lite._commands(commands) if c.source_path)
        for file in observed['files']:
            paths = {file['path'], file['old_path']} - {None}
            check('scope', not (paths & sources) and not any(
                PurePosixPath(p).name in {'.gitignore', '.gitattributes'} for p in paths)
                and not file['symlink'] and not file['submodule']
                and file['old_mode'] in {'000000', '100644', '100755'}
                and file['new_mode'] in {'000000', '100644', '100755'},
                'unsupported_scope: ' + file['path'])
        oids = {oid for _, pair in observed['versions'] for oid in pair if set(oid) != {'0'}}
        blobs = gate_lite._blobs(Path(worktree.path), oids, deadline)
        for file, (old, new) in observed['versions']:
            before = blobs.get(old, b'').decode('utf-8')
            after = blobs.get(new, b'').decode('utf-8')
            check('text', '\0' not in before + after and not file['binary'], 'binary_file')
            lines, added = gate_lite._added_lines(before, after)
            check('diff_check', not any(lines[i].rstrip('\r\n').endswith((' ', '\t'))
                  or lines[i].startswith(('<<<<<<<', '>>>>>>>'))
                  or lines[i].rstrip('\r\n') == '=======' for i in added),
                  'whitespace_or_conflict_markers')
            check('secrets', gate_lite._secret_line(after, added) is None, 'secret_suspected')
            old_tests, old_skips = gate_lite._counts(file['old_path'] or file['path'], before)
            new_tests, new_skips = gate_lite._counts(file['path'], after)
            check('test_floor', new_tests >= old_tests and new_skips <= old_skips, 'test_floor')
        selected = gate_lite._commands(commands)
        check('project_commands', bool(selected), 'no_commands')
        cache = Path(os.environ.get('ORCEXE_STATE_DIR') or Path.home() / '.cache/orcexe') / 'gate'
        cache.mkdir(parents=True, exist_ok=True, mode=0o700)
        cache_dir = Path(tempfile.mkdtemp(dir=str(cache))).resolve()
        checkout = gate_lite._checkout(repo_path, observed['tree'], cache_dir, {}, deadline)
        try:
            # F5: python3/python commands run with the base commit's declared interpreter.
            status, detail = gate_lite._project(selected, checkout, directory / 'gate',
                                                command_timeout_s, deadline, cache_dir,
                                                repo_path, worktree.base_commit)
        except gate_lite._ConfigError as error:
            status, detail = 'fail', 'invalid ' + str(error)
        check('project_checks', status == 'pass', detail)
        result['verdict'] = 'passed'
    except ValueError:
        if not result['checks'] or result['checks'][-1]['status'] == 'pass':
            result['checks'].append(dict(id='observation', status='fail', detail='observation_unavailable'))
    except Exception:
        result['checks'].append(dict(id='observation', status='fail', detail='observation_unavailable'))
    finally:
        if cache_dir is not None:
            try:
                shutil.rmtree(cache_dir)
            except OSError:
                result['verdict'] = 'failed'
                result['checks'].append(dict(id='cleanup', status='fail', detail='cleanup_unavailable'))
        isolation.annotate(result)
        safe_write_json(directory / 'gate.json', result, mode=0o600)
    return result
