"""Model-free history and direction evidence from committed Git objects."""

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import time
from types import FunctionType

from orcexe import fsio, intake, proc

FIX = re.compile(r'\b(?:fix|fixes|fixed|hotfix|revert|reverts|reverted)\b', re.I)
REVERT = re.compile(r'\b(?:this\s+)?reverts?\s+(?:commit\s+)?([0-9a-f]{40})\b', re.I)
EMAIL = re.compile(r'[^\s<>"/\\]+@[^\s<>"/\\]+')
COMMON_WORDS = frozenset('a an the and or but not for from to of in on at by with as is are was were be been '
                         'will can may must should would could do does did done has have had it its this that '
                         'these those we you they he she us them our your their my me all any each other '
                         'new old more less first last next previous one two three four five six seven '
                         'add added change changes fix fixed build test run map main master release '
                         'data code project history direction read write use used user users support '
                         'make made get set value values return true false null none error errors '
                         'time long short good best better open close small large public private '
                         'update updated remove removed branch commit path file files plan report '
                         'mark bill rose joy grace hope faith dawn amber brown gray white green black '
                         'blue red summer autumn winter april june august march'.split())


def _allowed(path):
    return not (intake._secret(path) or intake._excluded(path) or intake._own_state(path))


def _date(timestamp):
    return datetime.fromtimestamp(int(timestamp), timezone.utc).date()


def _clean(value, identities, field=None):
    # Structural data and fixed schema values are not prose. Preserve paths
    # verbatim, even when a component is an identity or resembles an email.
    if field in ('schema', 'head', 'head_date', 'week', 'sha', 'date', 'reverted',
                 'last_commit_date', 'last_changed', 'reason', 'kind',
                 'path', 'dir', 'single_owner_dirs'):
        return value
    if isinstance(value, str):
        # Git identities are observed across all refs, including merge commits.
        # Unicode word boundaries protect longer words while allowing an
        # apostrophe suffix. Keep separator variants used in branch names.
        for identity in identities:
            pattern = r'[\s._/-]+'.join(re.escape(part) for part in identity.split())
            sensitive = len(identity.split()) == 1 and (len(identity) < 4
                                                        or identity.casefold() in COMMON_WORDS)
            value = re.sub(r'(?<!\w)' + pattern + r'(?!\w)', '[REDACTED:pii]', value,
                           flags=0 if sensitive else re.I)
        return intake._clean(EMAIL.sub('[REDACTED:pii]', value))
    if isinstance(value, list):
        return [_clean(item, identities) for item in value]
    if isinstance(value, dict):
        return {key: _clean(item, identities, key) for key, item in value.items()}
    return value


def _branches(repo, git):
    _, raw = git('for-each-ref', '--format=%(refname)%00%(symref)%00%(committerdate:unix)',
                 'refs/heads/', 'refs/remotes/')
    refs = []
    for line in raw.splitlines():
        ref, symbolic, stamp = line.split(b'\0')
        refs.append((os.fsdecode(ref), os.fsdecode(symbolic), stamp))
    default = next((target for ref, target, _ in refs if ref == 'refs/remotes/origin/HEAD' and target), None)
    names = {ref for ref, _, _ in refs}
    if not default:
        default = next((ref for ref in ('refs/heads/main', 'refs/heads/master') if ref in names), None)
    if not default:
        _, raw = git('symbolic-ref', '-q', 'HEAD', allow_failure=True)
        default = os.fsdecode(raw).strip()
    if not default or default not in names:
        return []
    branches = []
    for ref, symbolic, stamp in refs:
        if symbolic or ref == default:
            continue
        _, count = git('rev-list', '--count', ref, '^' + default)
        ahead = int(count)
        if ahead:
            name = ref[len('refs/heads/'):] if ref.startswith('refs/heads/') else ref[len('refs/remotes/'):]
            branches.append(dict(name=name, last_commit_date=_date(stamp).isoformat(), ahead=ahead))
    return sorted(branches, key=lambda item: (-datetime.fromisoformat(item['last_commit_date']).toordinal(), item['name']))


def _github(url):
    return re.match(r'^(?:[a-z][a-z0-9+.-]*://(?:[^/@\s]+@)?github\.com(?::[0-9]+)?(?:[/?#]|$)'
                    r'|(?:[^@/:\s]+@)?github\.com:)', url, re.I) is not None


def _issues(repo, requested, git):
    def unseen(reason):
        return dict(status='not_seen', reason=reason, items=[])
    if not requested:
        return unseen('not_requested')
    _, remotes = git('config', '--get-regexp', r'^remote\..*\.url$', allow_failure=True)
    if not any(_github(line.split(None, 1)[1].decode('utf-8', 'replace'))
               for line in remotes.splitlines() if len(line.split(None, 1)) == 2):
        return unseen('no_github_remote')
    binary = shutil.which(os.environ.get('ORCEXE_BIN_GH', 'gh'))
    if not binary:
        return unseen('gh_missing')
    chunks, size = [], 0

    def collect(data):
        nonlocal size
        size += len(data)
        if size > intake.FILE_CAP:
            raise ValueError('issue_output_limit')
        chunks.append(data)
    try:
        result = proc.run([binary, 'issue', 'list', '--state', 'open', '--json', 'title,labels', '--limit', '50'],
                          cwd=str(repo), env=dict(os.environ, GH_PROMPT_DISABLED='1'),
                          timeout_s=20, stall_s=20, kill_grace_s=.1,
                          on_stdout_bytes=collect, max_line_bytes=intake.FILE_CAP)
        if result.status in ('timeout', 'stalled'):
            return unseen('timeout')
        if result.returncode == 4:
            return unseen('gh_not_authenticated')
        if result.status != 'exited' or result.returncode or result.pipes_held:
            return unseen('gh_error')
        data = json.loads(b''.join(chunks))
        if not isinstance(data, list):
            return unseen('gh_error')
        items = []
        for item in data[:50]:
            if not isinstance(item, dict) or not isinstance(item.get('title'), str) or not isinstance(item.get('labels'), list):
                return unseen('gh_error')
            labels = item['labels']
            if any(not isinstance(label, dict) or not isinstance(label.get('name'), str) for label in labels):
                return unseen('gh_error')
            items.append(dict(title=item['title'], labels=[label['name'] for label in labels]))
        return dict(status='seen', reason=None, items=items)
    except (OSError, ValueError, TypeError, RecursionError):
        return unseen('gh_error')


def _unreleased(text):
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = re.match(r'^(#{1,6})\s+\[?Unreleased\]?(?:\s|$)', line, re.I)
        if match:
            level = len(match[1])
            end = index + 1
            while end < len(lines):
                heading = re.match(r'^(#{1,6})\s+', lines[end])
                if heading and len(heading[1]) <= level:
                    break
                end += 1
            return ''.join(lines[index:end])
    return None


def _status(text):
    inline = re.search(r'(?im)^\s*(?:\*\*)?Status(?:\*\*)?\s*:\s*(.+)$', text)
    if inline:
        return inline[1].strip()
    section = re.search(r'(?im)^##\s+Status\s*\n\s*([^\n]+)', text)
    return section[1].strip() if section and not section[1].lstrip().startswith('#') else None


def _docs(repo, git, head, head_date, unseen, deadline):
    _, raw = git('ls-tree', '-r', '-l', '-z', head)
    entries = {}
    for record in raw.split(b'\0'):
        if record:
            header, path = record.split(b'\t', 1)
            mode, kind, oid, size = header.split()
            path = os.fsdecode(path)
            if not intake._own_state(path):
                entries[path] = (mode.decode(), oid.decode(), int(size) if size != b'-' else 0)
    # Rebind only the selection predicate, retaining U1's exact secret-path/blob,
    # mode, byte-budget and batch-validation choke point without global mutation.
    namespace = dict(intake._fetch.__globals__,
                     _file_kind=lambda path: _allowed(path) and intake._doc(path))
    reader = FunctionType(intake._fetch.__code__, namespace, 'history_blob_reader')
    contents = reader(repo, entries, unseen, deadline)
    docs = []
    for path, blob in sorted(contents.items()):
        text = blob.decode('utf-8', 'replace')
        name = PurePosixPath(path).name
        kind = ('changelog_unreleased' if name.startswith('CHANGELOG') else 'roadmap' if name.startswith('ROADMAP')
                else 'todo' if name.startswith('TODO') else 'adr' if path.startswith('docs/adr/') else 'decision')
        if kind == 'changelog_unreleased':
            text = _unreleased(text)
            if text is None:
                continue
        _, changed = git('log', '-1', '--format=%ct', head, '--', path)
        changed = _date(changed.strip())
        docs.append(dict(path=path, kind=kind, last_changed=changed.isoformat(),
                         stale=changed < head_date - timedelta(days=180),
                         status=_status(text) if kind in ('adr', 'decision') else None,
                         text=text, truncated=False))
    return docs


def scan(repo, issues=False):
    """Return (main worktree, {u2, u3}); never inspect working-tree content."""
    started = time.monotonic()
    deadline = started + 30
    repo = intake.repository_root(repo, deadline)

    def git(*args, **kwargs):
        return intake._git(repo, *args, deadline=deadline, **kwargs)

    _, trees = git('worktree', 'list', '--porcelain', '-z')
    main = Path(os.fsdecode(trees.split(b'\0', 1)[0][9:]))
    code, raw = git('rev-parse', '--verify', 'HEAD', allow_failure=True)
    head = None if code else raw.decode('ascii').strip()
    _, raw = git('rev-parse', '--is-shallow-repository')
    shallow = raw.strip() == b'true'
    unseen = []
    if shallow:
        unseen.append(dict(reason='shallow_history'))
    if not head:
        unseen.append(dict(reason='no_head'))
    # Capture PII privately from all reachable refs, including excluded merges.
    _, raw = git('log', '--all', '-z', '--format=%an%x00%ae%x00%cn%x00%ce', *([head] if head else []))
    identities = sorted({s.decode('utf-8', 'replace').strip() for s in raw.split(b'\0') if s.strip()}, key=lambda s: (-len(s), s))
    records, paths = [], defaultdict(set)
    head_date = None
    if head:
        _, raw = git('show', '-s', '--format=%ct', head)
        head_date = _date(raw.strip())
        _, raw = git('log', '--no-merges', '-z', '--format=%H%x00%ct%x00%an%x00%ae%x00%s%x00%B', head)
        fields = raw.split(b'\0')[:-1]
        for i in range(0, len(fields), 6):
            sha, stamp, name, email, subject, message = fields[i:i + 6]
            records.append(dict(sha=sha.decode('ascii'), date=_date(stamp), timestamp=int(stamp), author=(name, email),
                                subject=subject.decode('utf-8', 'replace'), message=message.decode('utf-8', 'replace')))
        records.sort(key=lambda record: -record['timestamp'])
        # One raw NUL-delimited batch: headers distinguish even hash-shaped paths.
        _, raw = git('diff-tree', '--stdin', '--root', '-r', '--no-renames', '--raw', '-z',
                     input_data=('\n'.join(r['sha'] for r in records) + '\n').encode('ascii'))
        tokens = iter(raw.split(b'\0')[:-1])
        sha = None
        for token in tokens:
            if token.startswith(b':'):
                path = os.fsdecode(next(tokens))
                if _allowed(path):
                    paths[sha].add(path)
            else:
                sha = token.decode('ascii').strip()
    weeks, hotspots, owners = {}, {}, defaultdict(dict)
    authors = set()
    reverts = []
    for record in records:
        date, sha = record['date'], record['sha']
        if head_date - timedelta(days=180) <= date <= head_date:
            week = date.strftime('%G-W%V')
            entry = weeks.setdefault(week, dict(commits=0, dirs=Counter()))
            entry['commits'] += 1
            authors.add(record['author'])
            dirs = set()
            for path in paths[sha]:
                parent = str(PurePosixPath(path).parent)
                entry['dirs'][parent] += 1
                dirs.add(parent)
            for parent in dirs:
                owners[parent][sha] = record['author']
        if head_date - timedelta(days=90) <= date <= head_date:
            for path in paths[sha]:
                counts = hotspots.setdefault(path, [0, 0])
                counts[0] += 1
                counts[1] += bool(FIX.search(record['subject']))
        match = REVERT.search(record['message'])
        if match or re.match(r'^Revert\b', record['subject'], re.I) or re.search(r'\breverts?\s+(?:a\s+)?commit\b', record['message'], re.I):
            reverts.append(dict(sha=sha, date=date.isoformat(), subject=record['subject'],
                                reverted=match[1].lower() if match else None))
    branches = _branches(repo, git) if head else []
    u2 = dict(schema='orcexe.intake.u2.v1', head=head, head_date=head_date.isoformat() if head_date else None,
              shallow=shallow, phases=[dict(week=week, commits=item['commits'],
                    top_dirs=[dict(dir=parent, changes=count) for parent, count in sorted(item['dirs'].items(), key=lambda v: (-v[1], v[0]))[:3]])
                    for week, item in sorted(weeks.items())],
              hotspots=[dict(path=path, changes=counts[0], fix_commits=counts[1])
                        for path, counts in sorted(hotspots.items(), key=lambda v: (-v[1][0], -v[1][1], v[0]))[:20]],
              reverts=reverts[:50], unmerged_branches=branches,
              authors=dict(count=len(authors), single_owner_dirs=sorted(parent for parent, commits in owners.items()
                           if len(commits) >= 3 and len(set(commits.values())) == 1)), unseen=list(unseen))
    u3 = dict(schema='orcexe.intake.u3.v1', head=head,
              docs=_docs(repo, git, head, head_date, unseen, deadline) if head else [],
              recent_commits=[dict(sha=r['sha'], date=r['date'].isoformat(), subject=r['subject']) for r in records[:20]],
              unmerged_branches=branches, issues=_issues(repo, issues, git), unseen=unseen)
    result = _clean(dict(u2=u2, u3=u3), identities)
    result['u3']['issues']['status'] = u3['issues']['status']
    for doc in result['u3']['docs']:
        doc['truncated'] = len(doc['text']) > 4000
        doc['text'] = doc['text'][:4000]
    for item in result['u3']['issues']['items']:
        item['title'] = item['title'][:200]
    meta = dict(duration_ms=round((time.monotonic() - started) * 1000),
                generated_at=datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'))
    for doc in result.values():
        doc['meta'] = dict(meta)
    return main, result


def run_history(repo, issues=False):
    main, result = scan(repo, issues)
    directory = main / '.orcexe' / 'intake'
    for parent in (main / '.orcexe', directory):
        if parent.is_symlink():
            raise intake.IntakeError('output_symlink')
        parent.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    for name, doc in result.items():
        fsio.atomic_write_json(directory / (name + '.json'), doc, mode=0o600)
    return result


def history_command(args):
    try:
        result = run_history(args.repo or Path.cwd(), args.issues)
    except (OSError, ValueError):
        print(json.dumps({'error': 'History could not inspect repository'}) if args.json
              else 'History could not inspect repository')
        return 2
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json
          else 'Wrote .orcexe/intake/u2.json and u3.json')
    return 0
