"""Offline U1 discovery. Only allow-listed, bounded HEAD blobs enter parsers.

Project instructions are opaque fingerprints. The security pass separately
extracts declaration names from the few structured tool configuration files;
no hook commands, MCP arguments, environment values or prose are returned.
Unsupported syntax is explicit in unseen, rather than guessed or executed.
"""

import configparser
from bisect import bisect_right
from datetime import datetime, timezone
import fnmatch
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import time
import xml.etree.ElementTree as ET

from orcexe import fsio, gitutil, miniyaml, proc
from orcexe.gate_lite import _git_setup
from orcexe.redact import redact_text

try:
    import tomllib
except ImportError:  # Python 3.9/3.10: deliberately conservative subset below.
    tomllib = None

FILE_CAP = 256 * 1024
TOTAL_BUDGET = 4 * 1024 * 1024
GIT_OUTPUT_CAP = 12 * 1024 * 1024
EXCLUDED = frozenset(('node_modules', 'vendor', '.venv', 'venv', 'dist',
                      'build', 'Pods', '.build', 'third_party'))
LOCKS = ('package-lock.json', 'npm-shrinkwrap.json', 'pnpm-lock.yaml', 'yarn.lock',
         'bun.lock', 'bun.lockb', 'poetry.lock', 'uv.lock', 'Pipfile.lock',
         'Cargo.lock', 'go.sum', 'Gemfile.lock', 'Package.resolved', 'gradle.lockfile')
SECURITY_CONFIG = {'.mcp.json', '.cursor/mcp.json', '.vscode/mcp.json',
                   '.claude/settings.json', '.claude/settings.local.json',
                   '.codex/config.toml', '.cursor/hooks.json'}


class IntakeError(ValueError):
    """A bounded observation failed; never includes project values."""


def _git(repo, *args, deadline, input_data=None, allow_failure=False, disable_filters=()):
    argv, env = _git_setup()
    for key in disable_filters:
        argv.extend(['-c', key + '='])
    remaining = min(3, deadline - time.monotonic())
    if remaining <= 0:
        raise IntakeError('git_timeout')
    chunks, length = [], 0

    def collect(data):
        nonlocal length
        length += len(data)
        if length > GIT_OUTPUT_CAP:
            raise IntakeError('git_output_limit')
        chunks.append(data)

    result = proc.run(argv + list(args), cwd=str(repo), env=env,
                      timeout_s=remaining, stall_s=remaining, input_data=input_data,
                      on_stdout_bytes=collect, max_line_bytes=GIT_OUTPUT_CAP,
                      kill_grace_s=.1)
    if result.status != 'exited' or result.pipes_held:
        raise IntakeError('git_incomplete')
    if result.returncode and not allow_failure:
        raise IntakeError('git_failed')
    return result.returncode, b''.join(chunks)


def _secret(path):
    # Also block descendants of a secret-named directory, before any blob fetch.
    return any(part.lower().startswith('.env') or any(fnmatch.fnmatchcase(part.lower(), pat)
               for pat in ('*.pem', '*.key', 'id_rsa*', '*.p8', '*.p12',
                           'credentials*', 'secrets*', '.npmrc', '.pypirc', '.netrc'))
               for part in PurePosixPath(path).parts)


def _excluded(path):
    return bool(EXCLUDED.intersection(PurePosixPath(path).parts))


def _own_state(path):
    return path == '.orcexe' or path.startswith('.orcexe/')


def _instruction(path):
    p = PurePosixPath(path)
    return (p.name in ('CLAUDE.md', 'AGENTS.md', 'GEMINI.md', '.cursorrules')
            or path == '.github/copilot-instructions.md'
            or any(part in ('.claude', '.codex') for part in p.parts[:-1])
            or '.cursor/rules/' in path)


def _doc(path):
    return (path.startswith('docs/adr/') or 'decisions' in PurePosixPath(path).parts[:-1]
            or any(PurePosixPath(path).name.startswith(prefix)
                   for prefix in ('ROADMAP', 'TODO', 'CHANGELOG')))


def _ecosystem(path):
    name = PurePosixPath(path).name
    exact = {'package.json': 'node', 'pyproject.toml': 'python', 'setup.cfg': 'python',
             'Cargo.toml': 'rust', 'go.mod': 'go', 'Package.swift': 'swift',
             'project.yml': 'swift', 'Gemfile': 'ruby', 'pom.xml': 'java'}
    if name in exact:
        return exact[name]
    if fnmatch.fnmatchcase(name, 'requirements*.txt'):
        return 'python'
    if name.startswith('build.gradle'):
        return 'jvm'
    return None


def _file_kind(path):
    """Closed fetch allow-list. Presence-only files never request blob content."""
    if _instruction(path):
        return 'instruction'
    if path in SECURITY_CONFIG:
        return 'security'
    if _excluded(path):
        return None
    name = PurePosixPath(path).name
    if name == 'Gemfile' or name.startswith('requirements'):
        return None
    if _ecosystem(path):
        return 'manifest'
    if name in ('Makefile', 'justfile', 'Justfile', 'tox.ini'):
        return 'commands'
    if _ci_provider(path):
        return 'ci'
    return None


def _ci_provider(path):
    if path.startswith('.github/workflows/') and PurePosixPath(path).suffix in ('.yml', '.yaml'):
        return 'GitHub Actions'
    if path == '.gitlab-ci.yml':
        return 'GitLab CI'
    if path in ('.circleci/config.yml', '.circleci/config.yaml'):
        return 'CircleCI'
    if PurePosixPath(path).name in ('azure-pipelines.yml', '.travis.yml', 'Jenkinsfile', 'bitbucket-pipelines.yml'):
        return 'other'
    return None


def _fetch(repo, entries, unseen, deadline):
    """The only blob request choke point. Filter paths, modes and sizes first."""
    selected, budget = {}, 0
    secret_oids = {oid for path, (_, oid, _) in entries.items() if _secret(path)}
    for path, (mode, oid, size) in sorted(entries.items()):
        reason = None
        if _secret(path):
            reason = 'secret_path'
        elif mode == '120000':
            reason = 'symlink'
        elif mode == '160000':
            reason = 'gitlink'
        elif _file_kind(path):
            if oid in secret_oids:
                reason = 'secret_blob'
            elif size > FILE_CAP:
                reason = 'file_size_cap'
            elif budget + size > TOTAL_BUDGET:
                reason = 'total_byte_budget'
            else:
                selected[path] = oid
                budget += size
        if reason:
            unseen.append({'path': path, 'reason': reason})
    oids = sorted(set(selected.values()))
    if not oids:
        return {}
    _, raw = _git(repo, 'cat-file', '--batch', deadline=deadline,
                  input_data=('\n'.join(oids) + '\n').encode('ascii'))
    sizes = {oid: entries[path][2] for path, oid in selected.items()}
    blobs, offset = {}, 0
    for oid in oids:
        end = raw.find(b'\n', offset)
        fields = raw[offset:end].split()
        if end < 0 or len(fields) != 3 or fields[:2] != [oid.encode(), b'blob']:
            raise IntakeError('blob_unavailable')
        length = int(fields[2])
        start, stop = end + 1, end + 1 + length
        if sizes[oid] != length or raw[stop:stop + 1] != b'\n':
            raise IntakeError('blob_incomplete')
        blobs[oid] = raw[start:stop]
        offset = stop + 1
    if offset != len(raw):
        raise IntakeError('blob_incomplete')
    return {path: blobs[oid] for path, oid in selected.items()}


def _toml(text):
    if tomllib is not None:
        return tomllib.loads(text)
    # Safe fallback: accept simple tables and scalar/array assignments only.
    # Refuse complex TOML explicitly; never evaluate dynamic values or code.
    result, section, tables = {}, None, set()

    def scalar(value):
        if isinstance(value, list):
            return all(scalar(item) for item in value)
        return (type(value) in (str, bool, int, float)
                and not (isinstance(value, float) and not math.isfinite(value)))

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        table = re.fullmatch(r'\[([A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*)\]\s*(?:#.*)?', line)
        if table:
            if table[1] in tables:
                raise ValueError('duplicate_toml_table')
            tables.add(table[1])
            section = result
            for key in table[1].split('.'):
                section = section.setdefault(key, {})
                if not isinstance(section, dict):
                    raise ValueError('toml')
            continue
        item = re.fullmatch(r'([A-Za-z0-9_-]+)\s*=\s*(.+)', line)
        if not item:
            raise ValueError('unsupported_toml')
        value = item[2]
        if value.startswith("'") and value.endswith("'") and "'" not in value[1:-1]:
            value = value[1:-1]
        else:
            value = json.loads(value)
        if not scalar(value):
            raise ValueError('unsupported_toml_value')
        target = section if section is not None else result
        if item[1] in target:
            raise ValueError('toml')
        target[item[1]] = value
    return result


def _yaml(text):
    """Line-based block scalar handling plus the existing strict YAML subset.

    No aliases, tags, interpolation or expression evaluation. Unsupported YAML
    rejects the entire file so a malformed tail cannot leave partial evidence.
    """
    lines = text.splitlines()
    output, i = [], 0
    while i < len(lines):
        line = lines[i]
        if i == 0 and line.strip() == '---':
            output.append('')
            i += 1
            continue
        block = re.fullmatch(r'(\s*(?:-\s+)?[\w.-]+:\s*)([|>])([-+]?)\s*(?:#.*)?', line)
        if block:
            indent = len(line) - len(line.lstrip())
            j = i + 1
            while j < len(lines) and (not lines[j].strip() or len(lines[j]) - len(lines[j].lstrip()) > indent):
                j += 1
            body = lines[i + 1:j]
            width = min((len(v) - len(v.lstrip()) for v in body if v.strip()), default=indent + 1)
            body = [v[width:] if v.strip() else '' for v in body]
            value = ('\n' if block[2] == '|' else ' ').join(body)
            if block[3] != '-':
                value = value.rstrip('\n') + '\n'
            output.append(block[1] + json.dumps(value))
            output.extend('' for _ in body)
            i = j
        else:
            output.append(line)
            i += 1
    data = miniyaml.loads('\n'.join(output))
    if not isinstance(data, dict):
        raise ValueError('yaml_mapping')
    return data


def _json(text):
    def pairs(items):
        data = {}
        for key, value in items:
            if key in data:
                raise ValueError('duplicate_key')
            data[key] = value
        return data
    data = json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('json_constant')))
    if not isinstance(data, dict):
        raise ValueError('json_mapping')
    return data


def _json_members(text, start=0):
    """Offsets of object members in already validated JSON, without guessing keys."""
    decoder = json.JSONDecoder()
    space = re.compile(r'\s*')
    offset = space.match(text, start).end() + 1  # opening brace
    while True:
        offset = space.match(text, offset).end()
        if text[offset] == '}':
            return
        key_start = offset
        key, offset = decoder.raw_decode(text, offset)
        offset = space.match(text, offset).end() + 1  # colon
        value_start = space.match(text, offset).end()
        _, offset = decoder.raw_decode(text, value_start)
        yield key, key_start, value_start
        offset = space.match(text, offset).end()
        if text[offset] == '}':
            return
        offset += 1  # comma


def _line(text, pattern):
    for number, value in enumerate(text.splitlines(), 1):
        if re.search(pattern, value):
            return number
    return 1


def _kind(name):
    for kind, pattern in (('format', r'format|fmt|prettier'), ('lint', r'lint|ruff|mypy|eslint|check'),
                          ('test', r'test|pytest|spec|tox'), ('build', r'build|compile|bundle|pack')):
        if re.search(pattern, name, re.I):
            return kind
    return 'other'


def _command(path, line, name, *, argv=None, script=None):
    item = dict(id=path + ':' + str(line) + ':' + name, kind=_kind(name),
                source='file:' + path + ':' + str(line), verified=False)
    item.update({'argv': argv} if argv is not None else {'script': script})
    return item


def _manifest(path, text, entry, paths):
    """Return complete file-level evidence or raise, before mutating the report."""
    commands, roots = [], []
    name = PurePosixPath(path).name
    if name == 'package.json':
        data = _json(text)
        scripts = data.get('scripts', {})
        if not isinstance(scripts, dict) or not all(isinstance(v, str) for v in scripts.values()):
            raise ValueError('scripts')
        source_lines = {}
        newlines = [i for i, char in enumerate(text) if char == '\n']
        for key, _, start in _json_members(text):
            if key == 'scripts':
                source_lines = {k: bisect_right(newlines, position) + 1
                                for k, position, _ in _json_members(text, start)}
        for key, value in scripts.items():
            commands.append(_command(path, source_lines[key], key, script=value))
        workspaces = data.get('workspaces', [])
        if isinstance(workspaces, dict):
            workspaces = workspaces.get('packages', [])
        if not isinstance(workspaces, list) or not all(isinstance(v, str) for v in workspaces):
            raise ValueError('workspaces')
        parent = PurePosixPath(path).parent
        for candidate in paths:
            if PurePosixPath(candidate).name == 'package.json' and candidate != path:
                relative = PurePosixPath(candidate).parent
                try:
                    relative = relative.relative_to(parent).as_posix()
                except ValueError:
                    continue
                if any(PurePosixPath(relative).match(pattern.rstrip('/')) for pattern in workspaces):
                    roots.append(str(PurePosixPath(candidate).parent))
        metadata = data
    elif name in ('pyproject.toml', 'Cargo.toml'):
        data = _toml(text)
        metadata = data.get('project' if name == 'pyproject.toml' else 'package', {})
        if name == 'pyproject.toml':
            tool = data.get('tool', {})
            if not isinstance(tool, dict):
                raise ValueError('tool')
            if not metadata:
                metadata = tool.get('poetry', {})
            for runner in ('pytest', 'ruff', 'mypy'):
                if runner in tool:
                    if not isinstance(tool[runner], dict):
                        raise ValueError('runner_section')
                    args = ['python3', '-m', runner] + (['check', '.'] if runner == 'ruff' else [])
                    commands.append(_command(path, _line(text, r'^\s*\[tool\.' + runner + r'[.\]]'), runner, argv=args))
    elif name == 'setup.cfg':
        data = configparser.ConfigParser(interpolation=None)
        data.read_string(text)
        metadata = dict(data['metadata']) if data.has_section('metadata') else {}
    elif name == 'go.mod':
        module = re.search(r'(?m)^\s*module\s+(\S+)\s*$', text)
        metadata = {'name': module[1].strip('"')} if module else {}
        if not module:
            raise ValueError('go_module')
    elif name == 'project.yml':
        metadata = _yaml(text)
    elif name == 'Package.swift':
        metadata = {}
        match = re.search(r'\bPackage\s*\(\s*name\s*:\s*"([^"\n]+)"', text)
        if match:
            metadata['name'] = match[1]
    elif name == 'pom.xml':
        # ElementTree never resolves external entities. Reject DTDs altogether.
        if '<!DOCTYPE' in text or '<!ENTITY' in text:
            raise ValueError('xml_dtd')
        root = ET.fromstring(text)
        metadata = {}
        for child in root:
            key = child.tag.rsplit('}', 1)[-1]
            if key in ('artifactId', 'version') and child.text:
                metadata['name' if key == 'artifactId' else key] = child.text.strip()
    elif name.startswith('build.gradle'):
        metadata = {}
        match = re.search(r'(?m)^\s*version\s*(?:=\s*)?[\'"]([^\'"]+)[\'"]\s*$', text)
        if match:
            metadata['version'] = match[1]
    else:
        metadata = {}
    if not isinstance(metadata, dict):
        raise ValueError('metadata')
    for key in ('name', 'version'):
        if key in metadata and isinstance(metadata[key], str):
            entry[key] = metadata[key]
    return commands, roots


def _targets(path, text):
    commands = []
    if PurePosixPath(path).name == 'tox.ini':
        parser = configparser.ConfigParser(interpolation=None)
        parser.read_string(text)
        envs = set()
        for section in parser.sections():
            if section.startswith('testenv:'):
                envs.add(section[8:])
        if parser.has_option('tox', 'envlist'):
            envs.update(v for v in re.split(r'[,\s]+', parser.get('tox', 'envlist')) if v)
        for env in sorted(envs):
            commands.append(_command(path, _line(text, r'envlist|testenv:' + re.escape(env)), 'tox-' + env,
                                     argv=['tox', '-e', env]))
    else:
        just = PurePosixPath(path).name.lower() == 'justfile'
        for number, line in enumerate(text.splitlines(), 1):
            pattern = r'^([A-Za-z_][\w-]*)(?:[ \t]+[^:]*)?\s*:(?!=)' if just else r'^([A-Za-z_][\w. -]*)\s*:(?!=)'
            match = re.match(pattern, line)
            if not match:
                continue
            names = [match[1].split()[0]] if just else match[1].split()
            for name in names:
                commands.append(_command(path, number, name, argv=['just' if just else 'make', name]))
    return commands


def _ci(path, text):
    if PurePosixPath(path).name == 'Jenkinsfile':
        raise ValueError('unsupported_ci')
    data = _yaml(text)
    commands, runners, matrix = [], set(), {}

    def literals(value):
        values = value if isinstance(value, list) else [value]
        if any(isinstance(v, float) and not math.isfinite(v) for v in values):
            raise ValueError('nonfinite_yaml')
        return [v for v in values if type(v) in (str, int, float, bool)
                and not (isinstance(v, str) and ('${{' in v or '$' in v or '<<' in v))]

    def visit(value, in_matrix=False):
        if isinstance(value, dict):
            for key, item in value.items():
                if key == 'run' and isinstance(item, str):
                    # Source lookup advances independently below for repeated run steps.
                    commands.append(item)
                if key == 'runs-on':
                    runners.update(str(v) for v in literals(item))
                if in_matrix and key not in ('include', 'exclude') and not isinstance(item, dict):
                    matrix.setdefault(key, set()).update(literals(item))
                visit(item, in_matrix or key == 'matrix')
        elif isinstance(value, list):
            for item in value:
                visit(item, in_matrix)
    visit(data)
    run_lines = [i for i, line in enumerate(text.splitlines(), 1) if re.match(r'\s*(?:-\s+)?run\s*:', line)]
    candidates = [_command(path, run_lines[i] if i < len(run_lines) else 1, script.split('\n')[0], script=script)
                  for i, script in enumerate(commands)]
    return candidates, sorted(runners), {k: sorted(v, key=lambda x: (type(x).__name__, str(x)))
                                         for k, v in sorted(matrix.items())}


def _security(path, text):
    data = _toml(text) if path.endswith('.toml') else _json(text)
    hooks, mcp = [], []
    for key in ('hooks', 'notify'):
        if key in data and data[key]:
            value = data[key]
            if key == 'hooks' and not isinstance(value, dict):
                raise ValueError('hooks')
            hooks.append({'path': path, 'declarations': sorted(value) if isinstance(value, dict) else ['notify']})
    if path.endswith('mcp.json') or path == '.mcp.json':
        servers = data.get('mcpServers', data.get('servers', {}))
        if not isinstance(servers, dict):
            raise ValueError('mcp_servers')
        for name, config in sorted(servers.items()):
            if not isinstance(config, dict):
                raise ValueError('mcp_server')
            item = {'path': path, 'name': name}
            command = config.get('command')
            if command is not None:
                if not isinstance(command, str):
                    raise ValueError('mcp_command')
                basename = re.split(r'[/\\]', command)[-1]
                if not basename or re.search(r'\s', basename):
                    raise ValueError('unsupported_mcp_command')
                item['command'] = basename
            mcp.append(item)
    return hooks, mcp


def _clean(value):
    # Fields are allow-listed before this defense-in-depth output redaction.
    if isinstance(value, str):
        value = re.sub('[\ud800-\udfff]', '[REDACTED:unicode]', value)
        value = re.sub(r'(?:/(?:Users|home)/[^\s/"\']+|/root)(?:/[^\s"\']*)?|[A-Za-z]:[\\/]Users[\\/][^\s"\']+',
                       '[REDACTED:home]', value)
        return redact_text(value).replace('[REDACTED:email]', '[REDACTED:pii]')
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_clean(k): _clean(v) for k, v in value.items()}
    return value


def scan(repo):
    """Observe HEAD and status; return (main worktree, sanitized U1 document)."""
    started = time.monotonic()
    deadline = started + 9
    repo = Path(repo).resolve()
    code, root = _git(repo, 'rev-parse', '--show-toplevel', deadline=deadline, allow_failure=True)
    if code:
        raise IntakeError('not_git_repository')
    repo = Path(os.fsdecode(root.rstrip(b'\n')))
    _, trees = _git(repo, 'worktree', 'list', '--porcelain', '-z', deadline=deadline)
    first = trees.split(b'\0', 1)[0]
    if not first.startswith(b'worktree '):
        raise IntakeError('main_worktree_unavailable')
    main = Path(os.fsdecode(first[9:]))
    code, head = _git(repo, 'rev-parse', '--verify', 'HEAD', deadline=deadline, allow_failure=True)
    head = head.decode('ascii').strip() if not code else None
    # Git status may otherwise execute clean/process filters while checking
    # dirty files. Query key names only, then disable every configured driver.
    _, filter_keys = _git(repo, 'config', '--null', '--name-only', '--get-regexp',
                          r'^filter\..*\.(clean|smudge|process|required)$',
                          deadline=deadline, allow_failure=True)
    disabled = {os.fsdecode(key) for key in filter_keys.split(b'\0') if key}
    # An empty Git boolean is false: required drivers become optional too.
    disabled.update(key.rsplit('.', 1)[0] + '.required' for key in list(disabled))
    _, status = _git(repo, 'status', '--porcelain=v1', '-z', '--untracked-files=all',
                     '--ignore-submodules=all', deadline=deadline, disable_filters=sorted(disabled))
    changed, untracked = gitutil._porcelain(os.fsdecode(status))
    changed = [path for path in changed if not _own_state(path)]
    untracked = [path for path in untracked if not _own_state(path)]
    version = fsio.read_json(Path(__file__).resolve().parents[2] / '.claude-plugin/plugin.json')['version']
    report = dict(schema='orcexe.intake.u1.v1', stack=[], commands=[],
                  ci={'provider': 'none', 'workflow_files': [], 'runs-on': [], 'matrix': {}},
                  instructions=[], security={'hooks': [], 'mcp': [], 'risk': 'none'}, docs=[],
                  size={'tracked_file_count': 0, 'bytes': 0, 'languages': {}, 'package_roots': []},
                  meta=dict(head=head, dirty=bool(changed), untracked_count=len(untracked),
                            duration_ms=0, orcexe_version=version, generated_at='', unseen=[]))
    unseen = report['meta']['unseen']
    entries = {}
    if head:
        _, listing = _git(repo, 'ls-tree', '-r', '-l', '-z', head, deadline=deadline)
        if listing and not listing.endswith(b'\0'):
            raise IntakeError('tree_incomplete')
        for record in listing.split(b'\0')[:-1]:
            header, path = record.split(b'\t', 1)
            mode, kind, oid, size = header.split()
            path = os.fsdecode(path)
            if not _own_state(path):
                entries[path] = (mode.decode(), oid.decode(), 0 if size == b'-' else int(size))
    else:
        unseen.append({'path': 'HEAD', 'reason': 'no_head'})
    report['size']['tracked_file_count'] = len(entries)
    report['size']['bytes'] = sum(v[2] for v in entries.values())
    paths = {p for p, (mode, _, _) in entries.items() if mode in ('100644', '100755') and not _excluded(p) and not _secret(p)}
    for path, (_, _, size) in sorted(entries.items()):
        extension = PurePosixPath(path).suffix.lower() or '[none]'
        language = report['size']['languages'].setdefault(extension, {'files': 0, 'bytes': 0})
        language['files'] += 1
        language['bytes'] += size
    try:
        contents = _fetch(repo, entries, unseen, deadline)
    except (IntakeError, ValueError) as error:
        unseen.append({'path': 'HEAD', 'reason': str(error) if isinstance(error, IntakeError) else 'blob_incomplete'})
        contents = {}
    roots, providers = set(), set()
    security_unknown = not head or any(p in SECURITY_CONFIG for p in entries if p not in contents)
    for path in sorted(paths):
        _, _, size = entries[path]
        if _doc(path):
            report['docs'].append({'path': path, 'size': size})
        if _instruction(path) and path in contents:
            report['instructions'].append({'path': path, 'size': size, 'sha256': hashlib.sha256(contents[path]).hexdigest()})
        if any(part in ('.husky', '.githooks') for part in PurePosixPath(path).parts[:-1]) or PurePosixPath(path).name in ('.pre-commit-config.yaml', '.pre-commit-config.yml'):
            report['security']['hooks'].append({'path': path})
        ecosystem = _ecosystem(path)
        if ecosystem:
            parent = PurePosixPath(path).parent
            entry = {'path': path, 'ecosystem': ecosystem,
                     'lock_files': [str(parent / lock) for lock in LOCKS if str(parent / lock) in paths]}
            report['stack'].append(entry)
            roots.add(str(parent))
        provider = _ci_provider(path)
        if provider:
            providers.add(provider)
            report['ci']['workflow_files'].append(path)
        if path not in contents or (_instruction(path) and path not in SECURITY_CONFIG):
            continue
        try:
            text = contents[path].decode('utf-8')
            if path in SECURITY_CONFIG:
                hooks, mcp = _security(path, text)
                report['security']['hooks'].extend(hooks)
                report['security']['mcp'].extend(mcp)
            elif ecosystem:
                commands, workspaces = _manifest(path, text, entry, paths)
                report['commands'].extend(commands)
                roots.update(workspaces)
            elif provider:
                commands, runners, matrix = _ci(path, text)
                report['commands'].extend(commands)
                report['ci']['runs-on'] = sorted(set(report['ci']['runs-on']) | set(runners))
                for key, values in matrix.items():
                    report['ci']['matrix'][key] = sorted(set(report['ci']['matrix'].get(key, [])) | set(values),
                                                        key=lambda x: (type(x).__name__, str(x)))
            elif _file_kind(path) == 'commands':
                report['commands'].extend(_targets(path, text))
        except (ValueError, TypeError, KeyError, AttributeError, RecursionError, configparser.Error, ET.ParseError):
            unseen.append({'path': path, 'reason': 'malformed_or_unsupported'})
            if path in SECURITY_CONFIG:
                security_unknown = True
    xcode = {str(PurePosixPath(*PurePosixPath(p).parts[:i + 1]))
             for p in paths for i, part in enumerate(PurePosixPath(p).parts) if part.endswith('.xcodeproj')}
    for path in sorted(xcode):
        report['stack'].append({'path': path, 'ecosystem': 'swift', 'lock_files': []})
        roots.add(str(PurePosixPath(path).parent))
    # Only the repository-local key is queried, with includes disabled; the
    # forced execution hooksPath is bypassed solely for this read-only query.
    try:
        _, config = _git(repo, 'config', '--local', '--no-includes', '--get', 'core.hooksPath',
                         deadline=deadline, allow_failure=True)
        if config.strip():
            hook = {'path': 'git-config:core.hooksPath'}
            configured = os.fsdecode(config.rstrip(b'\n'))
            location = PurePosixPath(configured)
            if not location.is_absolute() and '..' not in location.parts and not re.match(r'^[A-Za-z]:|^~|^\\', configured):
                hook['hooks_path'] = configured
                known = {entry['path'] for entry in report['security']['hooks']}
                for path in sorted(paths):
                    if path.startswith(configured.rstrip('/') + '/') and path not in known:
                        report['security']['hooks'].append({'path': path})
            report['security']['hooks'].append(hook)
    except IntakeError:
        unseen.append({'path': 'git-config:core.hooksPath', 'reason': 'git_incomplete'})
        security_unknown = True
    report['ci']['provider'] = next(iter(providers)) if len(providers) == 1 else 'other' if providers else 'none'
    report['size']['package_roots'] = sorted(roots) if len(roots) > 1 else []
    if report['security']['hooks'] or report['security']['mcp']:
        report['security']['risk'] = 'present'
    elif security_unknown:
        report['security']['risk'] = 'unknown'
    report['stack'].sort(key=lambda entry: entry['path'])
    report['commands'].sort(key=lambda entry: (entry['source'], entry['id']))
    unseen.sort(key=lambda entry: (entry['path'], entry['reason']))
    report = _clean(report)
    report['meta']['duration_ms'] = round((time.monotonic() - started) * 1000)
    report['meta']['generated_at'] = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
    return main, report


def run_intake(repo):
    main, report = scan(repo)
    directory = main / '.orcexe' / 'intake'
    for parent in (main / '.orcexe', directory):
        if parent.is_symlink():
            raise IntakeError('output_symlink')
        parent.mkdir(mode=0o700, exist_ok=True)
    directory.chmod(0o700)
    fsio.atomic_write_json(directory / 'u1.json', report, mode=0o600)
    return report


def intake_command(args):
    try:
        report = run_intake(args.repo or Path.cwd())
    except (OSError, IntakeError, gitutil.GitError):
        print(json.dumps({'error': 'Intake could not inspect repository'}) if args.json
              else 'Intake could not inspect repository')
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) if args.json
          else 'Wrote .orcexe/intake/u1.json')
    return 0
