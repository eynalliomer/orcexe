"""Deterministic fast-lane evidence, independent of executor self reports.

VARSAYIM: the fast lane permits at most 50 added + deleted lines by default.
Executors must be stopped and lifecycle operations serialized before gating.
repo_path must identify the trusted main repository. Project commands are
trusted base definitions; repository execution uses OS write/network isolation.
Check order: run_status → controls → diff_collect → diff_check → scope →
size → code_unchanged → secrets → test_floor → project_checks.
Git controls must be fingerprinted by the caller before the executor starts.
Project commands run only in a private gate-owned base checkout with the verified
raw target bytes overlaid. Executor caches and ancestor directories never enter
that checkout. Main node_modules is a user-owned dependency directory, linked
read-only by the project command sandbox.
The snapshot hashes raw files without clean filters, hooks or diff drivers.
Git replacement objects and graft files are disabled; redirection metadata is
fingerprinted. Before commands run, every checkout blob must match the base
tree, except the target, which must match its verified bytes. PHP and Ruby
source edits are unverifiable in v0.1; # is always code in the C-like lexer.
Scope and content checks cover base..HEAD, base..index and the raw worktree.
Every distinct base-to-state file diff counts once (line counts add across
versions); allowed targets may differ between states, every variant is checked.
Untracked files ignored by the base commit's tracked .gitignore files are excluded.
Executor ignore rules and config cannot hide candidates. Other untracked files
fail scope except regular cache files in
.pytest_cache/, .mypy_cache/, .ruff_cache/, and *.pyc in __pycache__/ directories.
.gitignore/.gitattributes, symlinks and non-pyc files in __pycache__ are never
cache exemptions. Git metadata
must be the registered linked-worktree gitfile; nested .git entries are refused
without following them, including submodules.
Incomplete Git output or project logs over 1 MiB are not_seen, never accepted.
"""

import ast
from dataclasses import dataclass
import difflib
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import selectors
import stat
import subprocess
import tempfile
import time
import sys
import warnings

from orcexe.adapters.base import (_write_parent, safe_write_text, safe_write_json, safe_write_bytes)

from orcexe import isolation, miniyaml, proc
from orcexe.adapters.base import validate_run_result
from orcexe.envfilter import child_env
from orcexe.redact import redact_text


_IDS = ("run_status", "controls", "diff_collect", "diff_check", "scope", "size",
        "code_unchanged", "secrets", "test_floor", "project_checks")
_FAIL = dict(zip(_IDS, ("failed", "escalated", "failed", "needs_fix", "escalated",
                       "escalated", "escalated", "needs_fix", "needs_fix", "needs_fix")))
# T61: run warnings that make a completed run unacceptable in every lane: output over the
# adapter's line cap (events lost or raw) and an executor that ran a different model than routed.
UNCLEAN_RUN_WARNINGS = ("output_capped", "model_mismatch")
_SHA = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_SOURCES = {".orcexe/commands.yaml", "package.json", "pnpm-lock.yaml",
            "yarn.lock", "bun.lockb", "package-lock.json", "pyproject.toml",
            "pytest.ini", "setup.cfg", "conftest.py", "Makefile", "Cargo.toml",
            "go.mod", ".python-version"}


@dataclass(frozen=True)
class Command:
    id: str
    argv: list
    source_path: str = None


class _ConfigError(ValueError):
    """Only schema key names may leave command parsing."""


class _ObservationError(RuntimeError):
    pass


def _require(ok, key):
    if not ok:
        raise ValueError("invalid " + key)


def _path(value):
    return (isinstance(value, str) and bool(value) and "\0" not in value
            and not PurePosixPath(value).is_absolute()
            and str(PurePosixPath(value)) == value
            and ".." not in PurePosixPath(value).parts and value != "."
            and not re.match(r"^[A-Za-z]:|^\\", value))


def _commands(commands):
    if not isinstance(commands, list):
        raise _ConfigError("commands")
    seen = set()
    for command in commands:
        if not isinstance(command, Command):
            raise _ConfigError("commands")
        if (not isinstance(command.id, str) or not _ID.fullmatch(command.id)
                or redact_text(command.id) != command.id or command.id in seen):
            raise _ConfigError("commands.id")
        seen.add(command.id)
        if (not isinstance(command.argv, list) or not command.argv
                or not all(isinstance(x, str) and x and "\0" not in x
                           for x in command.argv)):
            raise _ConfigError("commands.argv")
        if command.source_path is not None and not _path(command.source_path):
            raise _ConfigError("commands.source_path")
    return commands


def _git_setup(index=None):
    env, _ = child_env()
    env = {k: v for k, v in env.items() if k in
           {"PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT"}}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1", GIT_ATTR_NOSYSTEM="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_GRAFT_FILE=os.devnull,
               GIT_ALLOW_PROTOCOL="", LC_ALL="C", LANG="C")
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    argv = ["git", "--no-replace-objects", "--no-pager", "--literal-pathspecs", "-c", "core.fsmonitor=false",
            "-c", "core.hooksPath=" + os.devnull, "-c", "core.attributesFile=" + os.devnull,
            "-c", "protocol.allow=never"]
    return argv, env


def _git_bytes(path, *args, index=None, deadline=None, allow_failure=False, input_data=None):
    argv, env = _git_setup(index)
    remaining = 10 if deadline is None else min(10, deadline - time.monotonic())
    if remaining <= 0:
        raise _ObservationError("total_timeout")
    # Drain bounded binary output without the polling sleep used by wait(timeout)
    # with redirected stdout. Preserve bytes and refuse incomplete observations.
    with tempfile.TemporaryFile() as err, tempfile.TemporaryFile() as incoming:
        if input_data is not None:
            incoming.write(input_data)
            incoming.seek(0)
        child = subprocess.Popen(argv + list(map(str, args)), cwd=str(path), env=env,
                                 stdin=subprocess.DEVNULL if input_data is None else incoming, stdout=subprocess.PIPE, stderr=err)
        chunks, length = [], 0
        end = time.monotonic() + remaining
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(child.stdout, selectors.EVENT_READ)
                while True:
                    if not selector.select(max(0, end - time.monotonic())):
                        raise _ObservationError("git_timeout")
                    data = os.read(child.stdout.fileno(), 65536)
                    if not data:
                        break
                    length += len(data)
                    if length > 16 * 1024 * 1024:
                        raise _ObservationError("git_output_limit")
                    chunks.append(data)
            code = child.wait(timeout=max(.001, end - time.monotonic()))
            if code and not allow_failure:
                raise _ObservationError("git_observation_incomplete")
            return code, b"".join(chunks)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait()
            child.stdout.close()


def _git(path, *args, index=None, deadline=None, allow_failure=False, nul=False):
    code, data = _git_bytes(path, *args, index=index, deadline=deadline,
                            allow_failure=allow_failure)
    text = data.decode("utf-8").rstrip("\n")
    if nul and text and not text.endswith("\0"):
        raise _ObservationError("git_nul_incomplete")
    return code, text


def _main_metadata(repo_path):
    """Resolve trusted main metadata using files only."""
    main = Path(repo_path).resolve()
    while not os.path.lexists(main / ".git"):
        if main == main.parent:
            raise _ObservationError("main_git_metadata_missing")
        main = main.parent
    common = main / ".git"
    if common.is_file():
        raw = common.read_text().strip()
        if not raw.startswith("gitdir: "):
            raise _ObservationError("main_git_metadata_invalid")
        gitdir = (main / raw[8:]).resolve()
        common = (gitdir / (gitdir / "commondir").read_text().strip()).resolve()
    return common.parent, common


def _control_locations(repo_path, worktree):
    """Never start Git before controls pass."""
    main, common = _main_metadata(repo_path)
    path = Path(worktree.path)
    gitfile = path / ".git"
    for registration in (common / "worktrees").iterdir():
        expected = b"gitdir: " + os.fsencode(str(registration)) + b"\n"
        backlink = registration / "gitdir"
        if (backlink.is_file() and backlink.read_bytes() == os.fsencode(str(gitfile)) + b"\n"
                or gitfile.is_file() and not gitfile.is_symlink() and gitfile.read_bytes() == expected):
            return main, common, registration
    raise _ObservationError("worktree_registration_missing")


def _control_hash(path):
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "absent"
    if stat.S_ISLNK(mode):
        if not path.is_file():
            raise _ObservationError("nonregular_control_link")
        # Bind both pointer and contents; changing a symlink target's bytes
        # must not preserve a pre-executor fingerprint.
        data = b"symlink:" + os.fsencode(os.readlink(path)) + b"\0" + path.read_bytes()
    elif stat.S_ISREG(mode):
        data = path.read_bytes()
    else:
        data = b"nonregular:" + str(stat.S_IFMT(mode)).encode()
    return hashlib.sha256(data).hexdigest()


def snapshot_controls(repo_path, worktree) -> dict:
    """Fingerprint Git execution controls before starting an executor; no Git calls.

    Keys are metadata-root-relative (plus registration identity); values are
    SHA256 or 'absent'. Symlinks/nonregular controls are type-fingerprinted so
    replacing a regular control cannot preserve its identity.
    """
    _, common, registration = _control_locations(repo_path, worktree)
    controls = {"common/" + name: _control_hash(common / name) for name in
                ("config", "config.worktree", "info/attributes", "info/exclude",
                 "info/sparse-checkout", "info/grafts", "shallow", "objects/info/alternates")}
    for name in ("config.worktree", "commondir", "gitdir", "shallow", "objects/info/alternates"):
        controls["gitdir/" + name] = _control_hash(registration / name)
    controls["worktree/.git"] = _control_hash(Path(worktree.path) / ".git")
    controls["registration/" + str(registration)] = hashlib.sha256(
        os.fsencode(str(common))).hexdigest()
    # Walk controls without following symlinked directories. Bind all info
    # files, including worktree-local attributes/grafts and future controls.
    for label, metadata in (("common", common), ("gitdir", registration)):
        for name in ("hooks", "info", "refs/replace"):
            folder = metadata / name
            prefix = label + "/" + name
            controls[prefix] = _control_hash(folder)
            if folder.is_dir() and not folder.is_symlink():
                for root, dirs, files in os.walk(folder, followlinks=False):
                    for entry in files + dirs:
                        file = Path(root) / entry
                        controls[prefix + "/" + file.relative_to(folder).as_posix()] = _control_hash(file)
        # Ordinary branch packing is not a redirection. Fingerprint only the
        # replacement entries, retaining a symlink's pointer as well as bytes.
        packed = metadata / "packed-refs"
        if packed.is_file():
            lines, replacement = [], False
            for line in packed.read_bytes().splitlines(keepends=True):
                if line.startswith(b"^") and replacement:
                    lines.append(line)
                    continue
                fields = line.split()
                replacement = len(fields) == 2 and fields[1].startswith(b"refs/replace/")
                if replacement:
                    lines.append(line)
            pointer = b"symlink:" + os.fsencode(os.readlink(packed)) + b"\0" if packed.is_symlink() else b""
            controls[label + "/packed-replace"] = hashlib.sha256(pointer + b"".join(lines)).hexdigest()
        elif os.path.lexists(packed):
            controls[label + "/packed-replace"] = _control_hash(packed)
        else:
            controls[label + "/packed-replace"] = hashlib.sha256(b"").hexdigest()
    return controls


def _blobs(path, oids, deadline):
    if not oids:
        return {}
    _require(all(_SHA.fullmatch(oid) for oid in oids), 'blob_oid')
    _, data = _git_bytes(path, 'cat-file', '--batch', deadline=deadline,
                         input_data=('\n'.join(sorted(oids)) + '\n').encode('ascii'))
    blobs, cursor = {}, 0
    for oid in sorted(oids):
        end = data.index(b'\n', cursor)
        actual, kind, length = data[cursor:end].split()
        if actual.decode('ascii') != oid or kind != b'blob':
            raise _ObservationError('blob_unavailable')
        start, end = end + 1, end + 1 + int(length)
        if end >= len(data) or data[end:end + 1] != b'\n':
            raise _ObservationError('blob_incomplete')
        blobs[oid] = data[start:end]
        cursor = end + 1
    if cursor != len(data):
        raise _ObservationError('blob_incomplete')
    return blobs


def _detect(repo_path, base_commit, deadline=None):
    _require(isinstance(base_commit, str) and _SHA.fullmatch(base_commit), "base_commit")
    main, _ = _main_metadata(repo_path)
    override = main / ".orcexe" / "commands.yaml"
    if os.path.lexists(override):
        try:
            data = miniyaml.load_path(override)
        except (ValueError, OSError, UnicodeError):
            raise _ConfigError("commands.yaml") from None
        if not isinstance(data, dict):
            raise _ConfigError("commands.yaml")
        for key in data:
            if key not in {"version", "commands", "python"}:
                raise _ConfigError("commands.yaml." + redact_text(str(key))[:64])
        if "python" in data:
            _python_value(data["python"])
        if type(data.get("version")) is not int or data["version"] != 1:
            raise _ConfigError("version")
        if not isinstance(data.get("commands"), list):
            raise _ConfigError("commands")
        commands = []
        for item in data["commands"]:
            if not isinstance(item, dict) or set(item) != {"id", "argv"}:
                key = next((k for k in ("id", "argv") if not isinstance(item, dict)
                            or k not in item), None)
                if key is None:
                    key = redact_text(str(next(k for k in item if k not in {"id", "argv"})))[:64]
                raise _ConfigError("commands." + key)
            commands.append(Command(item["id"], item["argv"], ".orcexe/commands.yaml"))
        return _commands(commands)

    _, listing = _git(repo_path, "ls-tree", "-r", "--name-only", "-z", base_commit,
                      nul=True, deadline=deadline)
    paths = set(listing.split("\0")[:-1])

    def read(path):
        return _git(repo_path, "show", base_commit + ":" + path, deadline=deadline)[1]

    commands = []
    if "package.json" in paths:
        try:
            data = json.loads(read("package.json"))
            scripts = data.get("scripts", {})
            if not isinstance(scripts, dict):
                raise ValueError()
        except (ValueError, AttributeError):
            raise _ConfigError("package.json.scripts") from None
        manager = next((name for lock, name in (("pnpm-lock.yaml", "pnpm"),
                       ("yarn.lock", "yarn"), ("bun.lockb", "bun")) if lock in paths), "npm")
        for name in ("test", "lint", "build"):
            if name in scripts:
                if not isinstance(scripts[name], str) or not scripts[name].strip():
                    raise _ConfigError("package.json.scripts." + name)
                commands.append(Command("package-" + name, [manager, "run", name], "package.json"))
    pytest_source = None
    for path in ("pyproject.toml", "pytest.ini", "setup.cfg"):
        if path in paths and re.search(
                r"(?m)^\s*\[(?:tool\.pytest(?:\.[^\]]+)?|pytest|tool:pytest)\]\s*$", read(path)):
            pytest_source = path
            break
    if pytest_source is None and "conftest.py" in paths:
        pytest_source = "conftest.py"
    if pytest_source:
        commands.append(Command("pytest", ["python3", "-m", "pytest", "-q"], pytest_source))
    elif any(re.fullmatch(r"tests/(?:.*/)?test_[^/]*\.py", path) for path in paths):
        argv = ["python3", "-m", "unittest", "discover", "-s", "tests"]
        if "tests/__init__.py" in paths:
            argv.extend(["-t", "."])
        commands.append(Command("unittest", argv, None))
    elif any(re.fullmatch(r"test_[^/]*\.py", path) for path in paths):
        commands.append(Command("unittest", ["python3", "-m", "unittest", "discover"], None))
    if "Makefile" in paths:
        text = read("Makefile")
        for name in ("test", "lint", "check"):
            if re.search(r"(?m)^" + name + ":", text):
                commands.append(Command("make-" + name, ["make", name], "Makefile"))
    if "Cargo.toml" in paths:
        commands.append(Command("cargo-test", ["cargo", "test"], "Cargo.toml"))
    if "go.mod" in paths:
        commands.append(Command("go-test", ["go", "test", "./..."], "go.mod"))
    return commands


def detect_commands(repo_path, base_commit) -> list[Command]:
    """Read base definitions, except the explicit main-worktree YAML override."""
    return _detect(repo_path, base_commit)


def _registered_gitfile(repo_path, path, deadline):
    """Validate through trusted main metadata before any Git call in path."""
    _, common = _main_metadata(repo_path)
    gitfile = path / ".git"
    if path.is_symlink() or path.resolve() != path or not gitfile.is_file() or gitfile.is_symlink():
        return False
    # Do not resolve or pass the executor-controlled gitfile pointer to Git.
    for registration in (common / "worktrees").iterdir():
        backlink = registration / "gitdir"
        if backlink.read_bytes() == os.fsencode(str(gitfile)) + b"\n":
            return gitfile.read_bytes() == b"gitdir: " + os.fsencode(str(registration)) + b"\n"
    return False


def _cache_file(name, mode):
    parts = PurePosixPath(name).parts
    if not stat.S_ISREG(mode) or parts[-1] in {".gitignore", ".gitattributes"}:
        return False
    return (any(p in {".pytest_cache", ".mypy_cache", ".ruff_cache"} for p in parts[:-1])
            or "__pycache__" in parts[:-1] and parts[-1].endswith(".pyc"))


def _raw_files(path, deadline):
    """Walk without ignore rules or symlink traversal; never enter .git."""
    names, metadata = [], False
    pending = [path]
    while pending:
        if time.monotonic() >= deadline:
            raise _ObservationError("total_timeout")
        with os.scandir(pending.pop()) as entries:
            for entry in entries:
                if time.monotonic() >= deadline:
                    raise _ObservationError("total_timeout")
                file = Path(entry.path)
                if entry.name.casefold() == ".git":
                    metadata = metadata or file != path / ".git"
                    continue
                if entry.is_dir(follow_symlinks=False):
                    pending.append(file)
                else:
                    names.append(file.relative_to(path).as_posix())
    return names, metadata


def _base_ignored(path, directory, base_entries, names, deadline):
    """Evaluate only immutable base .gitignore blobs in a private repository."""
    rules = {name: oid for name, (mode, oid) in base_entries.items()
             if PurePosixPath(name).name == '.gitignore' and mode in {'100644', '100755'}}
    if not rules or not names:
        return set()
    root = directory / 'base-ignore'
    root.mkdir()
    # No templates, hooks, filters, worktree config or info/exclude are copied.
    # _git_setup also removes environment-injected and global/system config.
    _git(root, 'init', '--quiet', '--template=', deadline=deadline)
    blobs = _blobs(path, set(rules.values()), deadline)
    for name, oid in rules.items():
        if time.monotonic() >= deadline:
            raise _ObservationError('total_timeout')
        if not _path(name):
            raise _ObservationError('unsafe_git_path')
        safe_write_bytes(root / name, blobs[oid])
    # check-ignore rejects literal pathspec magic. Prefix raw stdin filenames
    # with ./ so a leading colon cannot be interpreted as pathspec magic.
    code, data = _git_bytes(root, '--no-literal-pathspecs', '-c', 'core.excludesFile=' + os.devnull,
                            'check-ignore', '--no-index', '-z', '--stdin',
                            input_data=b''.join(b'./' + os.fsencode(n) + b'\0' for n in sorted(names)),
                            allow_failure=True, deadline=deadline)
    if code not in (0, 1) or data and not data.endswith(b'\0'):
        raise _ObservationError('git_observation_incomplete')
    records = data.split(b'\0')[:-1]
    if any(not n.startswith(b'./') for n in records):
        raise _ObservationError('git_observation_incomplete')
    ignored = {os.fsdecode(n[2:]) for n in records}
    if not ignored <= names or bool(ignored) != (code == 0):
        raise _ObservationError('git_observation_incomplete')
    return ignored


def _snapshot(path, directory, deadline, base):
    """Hash raw files without filters, preserving the real index byte for byte."""
    index = directory / "index"
    raw, metadata = _raw_files(path, deadline)
    _, listing = _git(path, "ls-files", "--stage", "-z", nul=True, deadline=deadline)
    _, base_listing = _git(path, 'ls-tree', '-r', '-z', base, nul=True, deadline=deadline)
    base_entries = {}
    for entry in base_listing.split('\0')[:-1]:
        info, name = entry.split('\t', 1)
        mode, kind, oid = info.split()
        base_entries[name] = (mode, oid)
    # Removing a base-tracked path from the index cannot make it ignorable.
    tracked, gitlinks = set(base_entries), {}
    for entry in listing.split("\0")[:-1]:
        info, name = entry.split("\t", 1)
        mode, oid, stage = info.split()
        if stage != "0":
            raise _ObservationError("unmerged_index")
        tracked.add(name)
        if mode == "160000":
            gitlinks[name] = oid
    # The raw walk still detects nested metadata inside ignored directories.
    # Only base rules may exclude untracked paths; never consult executor rules.
    untracked = set(raw) - tracked
    ignored = _base_ignored(path, directory, base_entries, untracked, deadline)
    untracked -= ignored
    unsafe = {name for name in untracked
              if not _cache_file(name, (path / name).lstat().st_mode)}
    names = sorted((set(raw) - ignored) | set(gitlinks))
    entries, inputs, pending, dirty_submodules = [], [], [], set()
    for name in names:
        if not _path(name):
            raise _ObservationError("unsafe_git_path")
        # A gitlink is opaque, never a reason to execute Git inside a nested repo.
        if any(parent.as_posix() in gitlinks for parent in PurePosixPath(name).parents):
            continue
        file = path / name
        if any((path / parent).is_symlink() for parent in PurePosixPath(name).parents):
            continue
        try:
            mode = file.lstat().st_mode
        except FileNotFoundError:
            continue
        if stat.S_ISDIR(mode) and name in gitlinks:
            entries.append(("160000", gitlinks[name], name))
            if any(n.startswith(name + "/") for n in raw) or os.path.lexists(file / ".git"):
                dirty_submodules.add(name)
            continue
        if name not in tracked and _cache_file(name, mode):
            continue
        if stat.S_ISLNK(mode):
            target = directory / ("link-" + str(len(inputs)))
            safe_write_bytes(target, os.fsencode(os.readlink(file)))
            inputs.append(str(target))
            pending.append(("120000", name))
        elif stat.S_ISREG(mode):
            target = directory / ("raw-" + str(len(inputs)))
            safe_write_bytes(target, file.read_bytes())
            inputs.append(str(target))
            pending.append(("100755" if mode & stat.S_IXUSR else "100644", name))
        elif name in tracked:
            raise _ObservationError("unsupported_file_kind")
    if inputs:
        _, hashes = _git(path, "hash-object", "-w", "--no-filters", "--", *inputs,
                         deadline=deadline)
        hashes = hashes.split("\n")
        if len(hashes) != len(pending) or not all(_SHA.fullmatch(x) for x in hashes):
            raise _ObservationError("incomplete_hashes")
        entries.extend((mode, oid, name) for (mode, name), oid in zip(pending, hashes))
    if entries:
        args = ["update-index", "--add"]
        for mode, oid, name in entries:
            args.extend(("--cacheinfo", mode + "," + oid + "," + name))
        _git(path, *args, index=index, deadline=deadline)
    else:
        _git(path, "read-tree", "--empty", index=index, deadline=deadline)
    return index, dirty_submodules, len(unsafe), metadata, {name: oid for mode, oid, name in entries if mode in {"100644", "100755"}}


def _collect(path, base, index, deadline, dirty_submodules, tree=None):
    args = ("diff", "--cached", "--no-ext-diff", "--no-textconv", "--no-relative", "--no-color",
            "--ignore-submodules=none", "--submodule=short", "--find-renames", "--no-abbrev",
            "--output-indicator-new=+", "--output-indicator-old=-", "--output-indicator-context= ")
    if tree is not None:
        args = tuple(arg for arg in args if arg != "--cached")
    _, raw = _git(path, *args, "--raw", "--numstat", "-z", base,
                  *([tree] if tree is not None else []), "--", index=index,
                  deadline=deadline, nul=True)
    records = raw.split("\0")[:-1]
    files, fingerprints = [], []
    cursor = 0
    while cursor < len(records) and records[cursor].startswith(":"):
        header = records[cursor]
        cursor += 1
        old, new, before, after, status = header[1:].split()
        first = records[cursor]
        cursor += 1
        name = records[cursor] if status.startswith(("R", "C")) else first
        cursor += int(status.startswith(("R", "C")))
        if not _path(first) or not _path(name) or status == "U":
            raise _ObservationError("unsafe_git_path_or_unmerged")
        fingerprints.append((header, first, name))
        files.append(dict(path=name, old_path=first if name != first else None,
                          status=status, old_mode=old, new_mode=new, binary=False,
                          symlink="120000" in (old, new), submodule="160000" in (old, new),
                          added=0, deleted=0))
    records = iter(records[cursor:])
    for file in files:
        added, deleted, name = next(records).split("\t", 2)
        if not name:  # -z renames carry two more NUL-delimited names.
            next(records)
            name = next(records)
        if name != file["path"]:
            raise _ObservationError("diff_record_mismatch")
        file.update(binary=added == "-" or deleted == "-",
                    added=0 if added == "-" else int(added),
                    deleted=0 if deleted == "-" else int(deleted))
    for name in sorted(dirty_submodules - {f["path"] for f in files}):
        fingerprints.append(("dirty_gitlink", name))
        files.append(dict(path=name, old_path=None, status="M", old_mode="160000",
                          new_mode="160000", binary=False, symlink=False, submodule=True,
                          added=0, deleted=0))
    return args, files, fingerprints


_TESTS = {
    ".py": r"(?m)^\s*(?:async\s+)?def\s+test_\w*\s*\(",
    ".js": r"\b(?:it|test|describe)\s*\(",
    ".go": r"(?m)^\s*func\s+Test\w*\s*\(",
    ".rs": r"#\s*\[\s*test\s*\]",
    ".java": r"@Test\b",
}
_SKIPS = re.compile(r"@unittest\.skip\w*\b|@pytest\.mark\.skip\w*\b|pytest\.skip\s*\("
                    r"|\bxfail\b|\.(?:skip|only)\s*\(|\b(?:xit|xdescribe)\s*\("
                    r"|\bt\.Skip\w*\s*\(|#\s*\[\s*ignore\b|@Disabled\b")


def _counts(name, text):
    suffix = Path(name).suffix.lower()
    suffix = {".ts": ".js", ".jsx": ".js", ".tsx": ".js", ".mjs": ".js",
              ".cjs": ".js", ".kt": ".java", ".kts": ".java"}.get(suffix, suffix)
    pattern = _TESTS.get(suffix)
    return (len(re.findall(pattern, text)) if pattern else 0,
            len(_SKIPS.findall(text)))


_DOC = frozenset('.md .markdown .rst .txt .adoc'.split())
_CLIKE = frozenset(('.c .h .cc .cpp .hpp .cs .java .kt .swift .go .rs .js .mjs '
                    '.cjs .jsx .ts .tsx').split())


def parse_python(text):
    """ast.parse scanned user source without leaking its SyntaxWarning/DeprecationWarning."""
    with warnings.catch_warnings():
        warnings.simplefilter('ignore', SyntaxWarning)
        warnings.simplefilter('ignore', DeprecationWarning)
        return ast.parse(text)


def _python_code(text):
    tree = parse_python(text)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (node.body and isinstance(node.body[0], ast.Expr)
                    and isinstance(node.body[0].value, ast.Constant)
                    and isinstance(node.body[0].value.value, str)):
                node.body = node.body[1:]
    return ast.dump(tree, include_attributes=False)


def _tokens(text, suffix):
    """Small conservative lexer. Preserve quoted strings and token boundaries."""
    tokens, cursor = [], 0
    while cursor < len(text):
        rest = text[cursor:]
        char = text[cursor]
        if char.isspace():
            cursor += 1
            continue
        if rest.startswith('//'):
            end = text.find('\n', cursor)
            cursor = len(text) if end < 0 else end + 1
            continue
        if rest.startswith('/*'):
            end = text.find('*/', cursor + 2)
            if end < 0 or '/*' in text[cursor + 2:end]:
                raise ValueError('unsupported_comment')
            cursor = end + 2
            continue
        if (char == '`' or suffix in {'.swift', '.kt'} and rest.startswith('"""')
                or suffix == '.rs' and re.match(r'(?:br|r)#+?"|(?:br|r)"', rest)
                or suffix in {'.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx'} and char == '/'):
            raise ValueError('unsupported_literal')
        if char in "'\"":
            # Ambiguous Rust lifetimes fail closed as unterminated strings.
            end = cursor + 1
            while end < len(text):
                if text[end] == '\\':
                    end += 2
                elif text[end] == char:
                    end += 1
                    break
                else:
                    end += 1
            else:
                raise ValueError('unterminated_string')
            tokens.append(text[cursor:end])
            cursor = end
        else:
            match = re.match(r'\w+|>>>=|===|!==|>>>|<<=|>>=|\*\*=|=>|==|!=|<=|>=|'
                             r'\+\+|--|&&|\|\||\?\?|\?\.|<<|>>|\*\*|\+=|-=|\*=|/=|'
                             r'%=|&=|\|=|\^=|::|->|:=|[^\s]', rest)
            tokens.append(match[0])
            cursor += len(match[0])
    return tokens


def _same_code(name, before, after):
    suffix = Path(name).suffix.lower()
    if suffix in _DOC:
        return True
    if suffix == '.py':
        return _python_code(before) == _python_code(after)
    if suffix in _CLIKE:
        return _tokens(before, suffix) == _tokens(after, suffix)
    raise ValueError('unsupported_source')


def _added_lines(before, after):
    old, new = before.splitlines(keepends=True), after.splitlines(keepends=True)
    added = set()
    for tag, _, _, start, end in difflib.SequenceMatcher(None, old, new, autojunk=False).get_opcodes():
        if tag in {'insert', 'replace'}:
            added.update(range(start, end))
    return new, added


def _secret_line(after, added):
    # Compare the complete redacted carrier to the complete new file. A changed
    # old-file line identifies a redacted span, including multiline carriers.
    redacted = redact_text(after)
    if redacted == after:
        return None
    lines = after.splitlines(keepends=True)
    for tag, start, end, _, _ in difflib.SequenceMatcher(
            None, lines, redacted.splitlines(keepends=True), autojunk=False).get_opcodes():
        if tag != 'equal':
            overlap = added.intersection(range(start, end))
            if overlap:
                return min(overlap) + 1
    return None


def _verify_checkout(repo_path, base, path, verified, deadline):
    """Bind the complete materialised file set to replacement-free tree OIDs."""
    _, listing = _git_bytes(repo_path, 'ls-tree', '-r', '-z', base, deadline=deadline)
    if listing and not listing.endswith(b'\0'):
        raise _ObservationError('base_tree_incomplete')
    expected = {}
    for entry in listing.split(b'\0')[:-1]:
        info, name = entry.split(b'\t', 1)
        mode, kind, oid = info.decode('ascii').split()
        name = name.decode('utf-8')
        if (not _path(name) or kind != 'blob' or mode not in {'100644', '100755', '120000'}
                or not _SHA.fullmatch(oid) or name in expected):
            raise _ObservationError('unsupported_base_tree_entry')
        expected[name] = (mode, oid)
    names, metadata = _raw_files(path, deadline)
    if metadata or set(names) != set(expected) or not set(verified).issubset(expected):
        raise _ObservationError('checkout_file_set_mismatch')
    inputs, oids = [], []
    with tempfile.TemporaryDirectory(dir=str(path.parent), prefix='verify-') as scratch:
        for name in sorted(expected):
            if time.monotonic() >= deadline:
                raise _ObservationError('total_timeout')
            mode, oid = expected[name]
            file = path / name
            actual = file.lstat().st_mode
            if mode == '120000':
                if not stat.S_ISLNK(actual) or name in verified:
                    raise _ObservationError('checkout_mode_mismatch')
                raw_link = Path(scratch) / str(len(inputs))
                safe_write_bytes(raw_link, os.fsencode(os.readlink(file)))
                file = raw_link
            elif (not stat.S_ISREG(actual)
                  or bool(actual & stat.S_IXUSR) != (mode == '100755')):
                raise _ObservationError('checkout_mode_mismatch')
            if name in verified:
                if file.read_bytes() != verified[name]:
                    raise _ObservationError('checkout_hash_mismatch')
                continue
            # --stdin-paths accepts Git C-quoted paths, including newlines,
            # quotes and non-ASCII bytes. Octal escapes avoid locale ambiguity.
            inputs.append(b'"' + b''.join(('\\%03o' % c).encode('ascii')
                                         for c in os.fsencode(file)) + b'"\n')
            oids.append(oid)
        if inputs:
            _, hashes = _git_bytes(repo_path, 'hash-object', '--no-filters', '--stdin-paths',
                                   input_data=b''.join(inputs), deadline=deadline)
            if hashes != ('\n'.join(oids) + '\n').encode('ascii'):
                raise _ObservationError('checkout_hash_mismatch')


def _checkout(repo_path, base, root, verified, deadline):
    """Materialise trusted base objects, never run checkout filters or hooks."""
    path = root / 'checkout'
    path.mkdir(mode=0o700)
    # Raw tree/blob plumbing never interprets attributes, filters or textconv.
    _, listing = _git_bytes(repo_path, 'ls-tree', '-r', '-z', base, deadline=deadline)
    if listing and not listing.endswith(b'\0'):
        raise _ObservationError('base_tree_incomplete')
    entries = []
    for entry in listing.split(b'\0')[:-1]:
        info, raw_name = entry.split(b'\t', 1)
        mode, kind, oid = info.decode('ascii').split()
        name = os.fsdecode(raw_name)
        if (not _path(name) or any(p.casefold() == '.git' for p in PurePosixPath(name).parts)
                or kind != 'blob' or mode not in {'100644', '100755', '120000'}
                or not _SHA.fullmatch(oid)):
            raise _ObservationError('unsupported_base_tree_entry')
        entries.append((name, mode, oid))
    links = {name for name, mode, _ in entries if mode == '120000'}
    if any(str(parent) in links for name, _, _ in entries for parent in PurePosixPath(name).parents):
        raise _ObservationError('unsafe_base_tree')
    _, blobs = _git_bytes(repo_path, 'cat-file', '--batch', deadline=deadline,
                          input_data=''.join(oid + '\n' for _, _, oid in entries).encode('ascii'))
    offset = 0
    for name, mode, oid in entries:
        end = blobs.find(b'\n', offset)
        header = blobs[offset:end].split()
        if end < 0 or len(header) != 3 or header[:2] != [oid.encode('ascii'), b'blob']:
            raise _ObservationError('blob_incomplete')
        size = int(header[2])
        offset = end + 1
        data = blobs[offset:offset + size]
        if len(data) != size or blobs[offset + size:offset + size + 1] != b'\n':
            raise _ObservationError('blob_incomplete')
        offset += size + 1
        target = path / name
        if mode == '120000':
            parent, leaf = _write_parent(target)
            try:
                os.symlink(os.fsdecode(data), leaf, dir_fd=parent)
            finally:
                os.close(parent)
        else:
            safe_write_bytes(target, data, mode=0o755 if mode == '100755' else 0o644)
    if offset != len(blobs):
        raise _ObservationError('blob_incomplete')
    for name, content in verified.items():
        target = path / name
        if target.is_symlink() or any((path / p).is_symlink() for p in PurePosixPath(name).parents if str(p) != '.'):
            raise _ObservationError('unsafe_target')
        safe_write_bytes(target, content, mode=stat.S_IMODE(target.stat().st_mode))
    _verify_checkout(repo_path, base, path, verified, deadline)
    main, _ = _main_metadata(repo_path)
    dependencies = main / 'node_modules'
    if dependencies.is_dir() and not os.path.lexists(path / 'node_modules'):
        # User-owned dependencies; project sandbox denies writes outside checkout.
        (path / 'node_modules').symlink_to(dependencies, target_is_directory=True)
    # Validate only after the dependency link exists. Resolve each component in
    # order: normalising "node_modules/.." first hides an escape through a link.
    blocked = {path / 'node_modules'}
    for name in links:
        if not _inside_copy(path, name, blocked):
            raise _ObservationError('unsafe_base_symlink')
    return path


def _inside_copy(root, relative, blocked=()):
    """Resolve links component by component, refusing any outside traversal."""
    current, pending, hops = root, list(Path(relative).parts), 0
    if Path(relative).is_absolute():
        return False
    while pending:
        part = pending.pop(0)
        if part == '.':
            continue
        current = current.parent if part == '..' else current / part
        if (current != root and root not in current.parents) or current in blocked:
            return False
        if current.is_symlink():
            hops += 1
            if hops > 40:
                return False
            destination = Path(os.readlink(current))
            if destination.is_absolute():
                return False
            pending = list(destination.parts) + pending
            current = current.parent
    return True


# F5: a declared project Python is resolved by version, never taken from PATH order.
_PYTHON_NAMES = frozenset(("python3", "python"))
_PYTHON_LOCATIONS = ("/usr/bin", "/usr/local/bin", "/opt/homebrew/bin")
_PYTHON_PROBE = 'import sys; print("%d.%d" % sys.version_info[:2]); print(sys.executable)'


def _python_value(value):
    """commands.yaml `python:` is a "major.minor" string, never a path."""
    if not (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]?\.[0-9]{1,3}", value)):
        raise _ConfigError("commands.yaml.python (interpreter: expected \"major.minor\")")
    return value


def _lower_bound(spec):
    """Lowest major.minor admitted by a PEP 440 specifier list, or None."""
    bounds = []
    for operator, major, minor in re.findall(r"(>=|~=|==|>)\s*(\d+)\.(\d+)", spec):
        bounds.append((int(major), int(minor) + (1 if operator == ">" else 0)))
    return "{}.{}".format(*min(bounds)) if bounds else None


def declared_python(repo_path, base_commit, deadline=None, checkout=None):
    """(version, source) from the main override or the base commit, never a worktree.

    Order: main .orcexe/commands.yaml `python:`, base .python-version, base
    requires-python (pyproject.toml) / python_requires (setup.cfg) lower bound.
    No declaration: (None, "PATH"). checkout may name a gate-owned copy already
    materialised and verified from base_commit; its regular root files are read
    instead of asking Git again.
    """
    if checkout is None:
        _require(isinstance(base_commit, str) and _SHA.fullmatch(base_commit), "base_commit")
    main, _ = _main_metadata(repo_path)
    override = main / ".orcexe" / "commands.yaml"
    if os.path.lexists(override):
        try:
            data = miniyaml.load_path(override)
        except (ValueError, OSError, UnicodeError):
            raise _ConfigError("commands.yaml") from None
        if isinstance(data, dict) and "python" in data:
            return _python_value(data["python"]), "commands.yaml"
    names = (".python-version", "pyproject.toml", "setup.cfg")
    if checkout is not None:
        root = Path(checkout)
        paths = {n for n in names if (root / n).is_file() and not (root / n).is_symlink()}

        def read(name):
            return (root / name).read_text(encoding="utf-8")
    else:
        _, listing = _git(repo_path, "ls-tree", "--name-only", "-z", base_commit,
                          nul=True, deadline=deadline)
        paths = set(listing.split("\0")[:-1])

        def read(name):
            return _git(repo_path, "show", base_commit + ":" + name, deadline=deadline)[1]

    if ".python-version" in paths:
        match = re.search(r"(?<![\d.])(\d+)\.(\d+)", read(".python-version"))
        if match:
            return match[1] + "." + match[2], ".python-version"
    for name, pattern in (("pyproject.toml", r"(?m)^\s*requires-python\s*=\s*[\"']([^\"'\n]*)[\"']"),
                          ("setup.cfg", r"(?m)^\s*python_requires\s*=\s*([^\n]*)$")):
        if name in paths:
            match = re.search(pattern, read(name))
            version = _lower_bound(match[1]) if match else None
            if version:
                return version, "requires-python"
    return None, "PATH"


def _probe_python(interpreter, cwd, env, timeout, deadline):
    """Run the interpreter inside the project sandbox: ("M.m", sys.executable) or None."""
    lines = []
    try:
        probe = isolation.run([interpreter, '-c', _PYTHON_PROBE], cwd=str(cwd), env=env,
                              on_line=lines.append, stall_s=None,
                              timeout_s=max(.001, min(timeout, deadline - time.monotonic())))
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = [line.strip() for line in lines if line.strip()]
    if (probe.status == 'exited' and probe.returncode == 0 and not probe.pipes_held
            and len(lines) == 2 and re.fullmatch(r"\d+\.\d+", lines[0])):
        return lines[0], lines[1]
    return None


def project_python(repo_path, base_commit, cwd, env, timeout, deadline, probe_path=True, checkout=None):
    """Resolve the interpreter python3/python commands must run with.

    Returns dict(declared, source, path, version, executable). path is None
    when undeclared (argv stays PATH python3) or when the declared version is
    unavailable (python commands are then not_seen). probe_path=False skips
    the identity probe of an undeclared PATH python3 (nothing is rewritten).
    """
    declared, source = declared_python(repo_path, base_commit, deadline, checkout)
    search = env.get("PATH", os.defpath)
    if declared is None:
        found = shutil.which("python3", path=search)
        probed = _probe_python(found, cwd, env, timeout, deadline) if found and probe_path else None
        return dict(declared=None, source="PATH", path=None, which=found,
                    version=probed[0] if probed else None,
                    executable=probed[1] if probed else "unavailable")
    candidates = []
    found = shutil.which("python" + declared, path=search)
    if found:
        candidates.append(os.path.abspath(found))
    for folder in _PYTHON_LOCATIONS:
        for name in ("python" + declared, "python3"):
            candidate = os.path.join(folder, name)
            if candidate not in candidates and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                candidates.append(candidate)
    for candidate in candidates:
        if time.monotonic() >= deadline:
            break
        probed = _probe_python(candidate, cwd, env, timeout, deadline)
        if probed and probed[0] == declared:
            return dict(declared=declared, source=source, path=candidate, which=candidate,
                        version=declared, executable=probed[1])
    return dict(declared=declared, source=source, path=None, which=None, version=None,
                executable="unavailable")


def python_argv(argv, interpreter):
    """Replace a python3/python argv[0] with the resolved absolute interpreter."""
    if interpreter and interpreter.get("path") and argv and argv[0] in _PYTHON_NAMES:
        return [interpreter["path"]] + list(argv[1:])
    return list(argv)


def python_unavailable(argv, interpreter):
    """Detail when a python command's declared interpreter cannot be found, else None."""
    if (interpreter and interpreter.get("declared") and not interpreter.get("path")
            and argv and argv[0] in _PYTHON_NAMES):
        return "interpreter_unavailable: python " + interpreter["declared"]
    return None


def with_path_identity(interpreter, cwd, env, timeout, deadline):
    """Add the historical PATH python3 identity (probed once when undeclared)."""
    if interpreter is not None and interpreter["declared"] is None:
        return dict(interpreter, path_python=interpreter["executable"])
    found = shutil.which("python3", path=env.get("PATH", os.defpath))
    probed = _probe_python(found, cwd, env, timeout, deadline) if found else None
    if interpreter is None:
        interpreter = dict(declared=None, source="PATH", path=None, which=found,
                           version=probed[0] if probed else None,
                           executable=probed[1] if probed else "unavailable")
    return dict(interpreter, path_python=probed[1] if probed else "unavailable")


def interpreters_log(interpreter):
    return ("gate sys.executable: {}\nPATH python3 sys.executable: {}\n"
            "project python: {} version {} source {}{}\n").format(
        sys.executable, interpreter.get("path_python", "unavailable"),
        interpreter.get("path") or interpreter.get("which") or "unavailable",
        interpreter.get("version") or "unavailable", interpreter["source"],
        " (declared {})".format(interpreter["declared"]) if interpreter.get("declared") else "")


# T55: project-check logs as written, keyed by (resolved gate directory, command id). L0
# reads runner counts from here, never back from disk where a leftover process could edit them.
_LOGS = {}


def _write_log(directory, identifier, text, mode=0o600):
    safe_write_text(directory / (identifier + ".log"), text, mode=mode)
    _LOGS[(str(Path(directory).resolve()), identifier)] = text


def project_log(directory, identifier):
    """The log text this process wrote for a project check, or None."""
    return _LOGS.get((str(Path(directory).resolve()), identifier))


def _project(commands, path, directory, timeout, deadline, gate_dir=None, repo_path=None, base=None):
    if not commands:
        return "not_seen", "no_commands"
    env, _ = child_env()
    interpreter = with_path_identity(
        project_python(repo_path, base, path, env, timeout, deadline) if repo_path is not None
        else None, path, env, timeout, deadline)
    safe_write_text(directory / "interpreters.log", redact_text(interpreters_log(interpreter)), mode=0o600)
    observations, missing = [], False
    for command in commands:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return "fail", command.id + ": total_timeout"
        unavailable = python_unavailable(command.argv, interpreter)
        if unavailable:
            missing = True
            observations.append(command.id + ": not_seen (" + unavailable + ")")
            _write_log(directory, command.id, "not_seen: " + unavailable + "\n", mode=0o600)
            continue
        command = Command(command.id, python_argv(command.argv, interpreter), command.source_path)
        if not shutil.which(command.argv[0], path=env.get("PATH", os.defpath)):
            missing = True
            observations.append(command.id + ": not_seen (binary_missing)")
            _write_log(directory, command.id,
                                   "not_seen: binary_missing\n", mode=0o600)
            continue
        output, output_bytes, overflow = [], 0, False

        def capture(line):
            nonlocal output_bytes, overflow
            if overflow:
                return
            output_bytes += len(line.encode("utf-8")) + 1
            if output_bytes > 1024 * 1024:
                overflow = True
                output.clear()
            else:
                output.append(line + "\n")

        limit = min(timeout, remaining)
        result = isolation.run(list(command.argv), cwd=str(path), env=env,
                          timeout_s=limit, stall_s=None,
                          on_line=capture, on_stderr_line=capture,
                          kill_grace_s=min(0.1, timeout), max_line_bytes=1048576)
        # Redact complete multiline carriers *before* taking the last 4 KiB.
        # If bounded collection was incomplete, publish no partial secret material.
        incomplete = overflow or result.truncated_lines
        log = ("not_seen: log_observation_incomplete\n" if incomplete else
               redact_text("".join(output)).encode("utf-8")[-4096:].decode("utf-8", errors="ignore"))
        _write_log(directory, command.id, log, mode=0o600)
        text = "".join(output)
        zero = (not incomplete and result.status == "exited" and not result.pipes_held and
                (re.search(r"Ran\s+0\s+tests?\b|NO TESTS RAN", text, re.I) or
                 (result.returncode == 5 and "no tests ran" in text.lower())))
        if zero:
            missing = True
            observations.append(command.id + ": not_seen (zero_tests)")
            continue
        if time.monotonic() >= deadline:
            return "fail", command.id + ": total_timeout"
        if result.status != "exited" or result.returncode != 0 or result.pipes_held:
            return "fail", command.id + ": " + (
                "timeout ({:g}s)".format(round(limit, 3)) if result.status == "timeout"
                else result.status if result.status != "exited"
                else "exit_nonzero_or_incomplete") + ("; write outside checkout denied"
                    if re.search(r"PermissionError|Operation not permitted|Permission denied|Read-only file system", text, re.I) else "")
        missing = missing or bool(incomplete)
        observations.append(command.id + (": not_seen (log_observation_incomplete)" if incomplete else ": pass"))
    if interpreter["declared"] and not interpreter["path"]:
        # A declared interpreter that cannot be found never lets the gate pass.
        missing = True
        unavailable = "interpreter_unavailable: python " + interpreter["declared"]
        if not any(unavailable in item for item in observations):
            observations.append("python: not_seen (" + unavailable + ")")
    return ("not_seen" if missing else "pass"), "; ".join(observations)


@isolation.guarded
def gate(*, repo_path, worktree, classification, run_result, run_dir,
         controls_before=None, commands=None, command_timeout_s=600, total_timeout_s=1800,
         diff_line_limit=50, added_byte_limit=20000, retry=False) -> dict:
    """Fail closed, never expose exception text, atomically replace gate.json.

    retry=True marks a later attempt in the same task worktree: the executor's
    per-attempt empty_diff warning then does not fail run_status, because
    diff_collect and every later check observe the cumulative tree against
    the base. `orcexe do` has a single attempt and never passes it.

    The caller owns run_dir and must make it writable. No API can persist a
    result to an unwritable/nonexistent device; persistence errors also return
    unverified and invalidate an existing receipt where the filesystem permits.
    """
    result = dict(schema='orcexe.gate.v1', run_id='invalid', verdict='unverified', checks=[],
                  diff=dict(files=[], added=0, deleted=0, base_commit=None, head=None),
                  escalation=None, warnings=[])
    blocked, gate_dir, temporary = False, None, None
    current, started = 'run_status', time.monotonic()

    def record(check, status, detail, start):
        nonlocal blocked
        result['checks'].append(dict(id=check, status=status,
            detail=redact_text(detail)[:300], duration_ms=max(0, int((time.monotonic() - start) * 1000))))
        if status == 'fail':
            result['verdict'] = _FAIL[check]
            result['escalation'] = 'lane' if _FAIL[check] == 'escalated' else None
        if status in {'fail', 'not_seen'}:
            blocked = True

    def unavailable(reason):
        index = next((i for i, c in enumerate(result['checks']) if c['status'] != 'pass'), len(_IDS) - 1)
        result['checks'][index].update(status='not_seen', detail=reason)
        for c in result['checks'][index + 1:]:
            c.update(status='skipped', detail='previous_check_incomplete')
        result.update(verdict='unverified', escalation=None)

    try:
        directory, path = Path(run_dir), Path(worktree.path)
        base = worktree.base_commit
        run_id = run_result.get('run_id') if isinstance(run_result, dict) else None
        if isinstance(run_id, str) and _ID.fullmatch(run_id) and redact_text(run_id) == run_id:
            result['run_id'] = run_id
        if isinstance(base, str) and _SHA.fullmatch(base):
            result['diff']['base_commit'] = base
        for key, value in (('command_timeout_s', command_timeout_s), ('total_timeout_s', total_timeout_s)):
            _require(type(value) in (int, float) and math.isfinite(value) and value > 0, key)
        _require(type(diff_line_limit) is int and diff_line_limit >= 0, 'diff_line_limit')
        _require(type(added_byte_limit) is int and added_byte_limit >= 0, 'added_byte_limit')
        _require(type(retry) is bool, 'retry')
        deadline = time.monotonic() + total_timeout_s
        changes, files, versions, verified = [], [], [], {}
        scope_problem = None
        for check in _IDS:
            current, started = check, time.monotonic()
            if blocked:
                record(check, 'skipped', 'previous_check_incomplete', started)
                continue
            try:
                status, detail = 'pass', 'observed'
                if check == 'run_status':
                    try:
                        validate_run_result(run_result)
                    except (ValueError, TypeError):
                        status, detail = 'fail', 'invalid_run_result'
                    else:
                        if run_result['status'] != 'completed':
                            status, detail = 'fail', run_result['status']
                        elif 'empty_diff' in run_result['warnings'] and not retry:
                            status, detail = 'fail', 'empty_diff'
                        else:
                            # T61: capped output or another model's work is never a clean run.
                            unclean = [w for w in UNCLEAN_RUN_WARNINGS if w in run_result['warnings']]
                            if unclean:
                                status, detail = 'fail', unclean[0]
                elif check == 'controls':
                    if controls_before is None:
                        status, detail = 'not_seen', 'controls_before_missing'
                    elif (not isinstance(controls_before, dict) or not controls_before
                          or not all(isinstance(k, str) and isinstance(v, str)
                                     and (v == 'absent' or re.fullmatch(r'[a-f0-9]{64}', v))
                                     for k, v in controls_before.items())):
                        status, detail = 'not_seen', 'controls_before_invalid'
                    else:
                        try:
                            same = snapshot_controls(repo_path, worktree) == controls_before
                        except Exception:
                            same = False
                        if not same:
                            status, detail = 'fail', 'git_controls_changed'
                elif check == 'diff_collect':
                    _require(isinstance(base, str) and _SHA.fullmatch(base), 'base_commit')
                    if not _registered_gitfile(repo_path, path, deadline):
                        scope_problem = 'worktree_git_metadata_changed'
                    else:
                        code, symbolic = _git(path, 'symbolic-ref', '-q', 'HEAD', deadline=deadline,
                                              allow_failure=True)
                        ref = 'refs/heads/' + worktree.branch
                        refs = ['HEAD^{commit}'] + ([ref] if not code and symbolic == ref else [])
                        _, resolved = _git(path, 'rev-parse', *refs, deadline=deadline)
                        hashes = resolved.splitlines()
                        head = hashes[0]
                        _require(bool(_SHA.fullmatch(head)), 'head')
                        result['diff']['head'] = head
                        if code or symbolic != ref or len(hashes) != 2 or hashes[1] != head:
                            scope_problem = 'head_branch_mismatch'
                        if head != base:
                            code, _ = _git(path, 'merge-base', '--is-ancestor', base, head,
                                           allow_failure=True, deadline=deadline)
                            if code:
                                scope_problem = 'base_not_ancestor'
                        temporary = tempfile.TemporaryDirectory(prefix='orcexe-gate-')
                        scratch = Path(temporary.name)
                        index, dirty, untracked, metadata, raw_oids = _snapshot(path, scratch, deadline, base)
                        # A snapshot-completion receipt also permits deterministic
                        # late-writer regression probes without timing guesses.
                        safe_write_text(scratch / 'head-index', head)
                        states = [(base, index, None), (base, None, None)]
                        if head != base:
                            states.append((base, None, head))
                        if head != base:
                            # Every parent-to-commit edge, including merge parents.
                            _, history = _git(path, 'rev-list', '--reverse', '--parents', base + '..' + head,
                                               deadline=deadline)
                            for entry in history.splitlines():
                                commit, *parents = entry.split()
                                states.extend((parent, None, commit) for parent in parents)
                        seen, merged = set(), {}
                        for against, observed, tree in states:
                            args, state_files, fingerprints = _collect(
                                path, against, observed, deadline, dirty if observed == index else set(), tree)
                            for file, fingerprint in zip(state_files, fingerprints):
                                if fingerprint in seen:
                                    continue
                                seen.add(fingerprint)
                                changes.append((file, observed, against, fingerprint))
                                key = (file['path'], file['old_path'], file['status'],
                                       file['old_mode'], file['new_mode'])
                                if key not in merged:
                                    merged[key] = dict(file)
                                else:
                                    for count in ('added', 'deleted'):
                                        merged[key][count] += file[count]
                                    for flag in ('binary', 'symlink', 'submodule'):
                                        merged[key][flag] |= file[flag]
                        files = list(merged.values())
                        if metadata:
                            scope_problem = 'nested_git_metadata'
                        elif untracked:
                            scope_problem = 'untracked_ignored:' + str(untracked)
                        # One binary cat-file batch reads immutable raw/commit blobs.
                        oids = {raw_oids[n] for n in classification.targets if n in raw_oids}
                        for file, _, _, fingerprint in changes:
                            if file['status'] == 'M' and not any(file[k] for k in ('symlink', 'submodule', 'binary')):
                                oids.update(fingerprint[0][1:].split()[2:4])
                        blobs = _blobs(path, oids, deadline)
                        verified = {n: blobs[raw_oids[n]] for n in classification.targets
                                    if n in raw_oids and raw_oids[n] in blobs}
                        for file, observed, against, fingerprint in changes:
                            if file['status'] == 'M' and not any(file[k] for k in ('symlink', 'submodule', 'binary')):
                                old_oid, new_oid = fingerprint[0][1:].split()[2:4]
                                before, after = blobs[old_oid], blobs[new_oid]
                                if b'\0' in before or b'\0' in after:
                                    file['binary'] = True
                                    merged[(file['path'], file['old_path'], file['status'],
                                            file['old_mode'], file['new_mode'])]['binary'] = True
                                else:
                                    before, after = before.decode('utf-8'), after.decode('utf-8')
                                    lines, added = _added_lines(before, after)
                                    versions.append((file['path'], before, after, lines, added))
                    result['diff'].update(files=[dict(f, path=redact_text(f['path']),
                        old_path=redact_text(f['old_path']) if f['old_path'] else None) for f in files],
                        added=sum(f['added'] for f in files), deleted=sum(f['deleted'] for f in files))
                    if not files and not scope_problem:
                        status, detail = 'fail', 'empty_diff'
                elif check == 'diff_check':
                    for _, _, _, lines, added in ([] if scope_problem else versions):
                        if any(lines[i].rstrip('\r\n').endswith((' ', '\t'))
                               or lines[i].startswith(('<<<<<<<', '>>>>>>>'))
                               or lines[i].rstrip('\r\n') == '=======' for i in added):
                            status, detail = 'fail', 'whitespace_or_conflict_markers'
                            break
                elif check == 'scope':
                    if scope_problem:
                        status, detail = 'fail', scope_problem
                    else:
                        sources = set(_SOURCES)
                        if commands is not None:
                            sources.update(c.source_path for c in _commands(commands) if c.source_path)
                        for file, *_ in changes:
                            paths = {file['path'], file['old_path']} - {None}
                            if paths & sources or any(PurePosixPath(p).name in {'.gitignore', '.gitattributes'}
                                                      for p in paths):
                                status, detail = 'fail', 'command_source_changed: ' + file['path']
                                break
                            if (len(classification.targets) != 1 or not paths.issubset(set(classification.targets))
                                    or file['status'] != 'M' or file['old_mode'] != file['new_mode']
                                    or file['binary'] or file['symlink'] or file['submodule']):
                                status, detail = 'fail', 'outside_plain_text_scope: ' + file['path']
                                break
                elif check == 'size':
                    added_bytes = sum(len(lines[i].encode('utf-8')) for *_, lines, added in versions for i in added)
                    if result['diff']['added'] + result['diff']['deleted'] > diff_line_limit:
                        status, detail = 'fail', 'diff_line_limit (VARSAYIM)'
                    elif added_bytes > added_byte_limit:
                        status, detail = 'fail', 'added_byte_limit'
                elif check == 'code_unchanged':
                    for name, before, after, _, _ in versions:
                        try:
                            equal = _same_code(name, before, after)
                        except (ValueError, SyntaxError):
                            status, detail = 'fail', 'code_change_unverifiable'
                            break
                        if not equal:
                            status, detail = 'fail', 'code_changed'
                            break
                elif check == 'secrets':
                    for name, _, after, _, added in versions:
                        number = _secret_line(after, added)
                        if number is not None:
                            status, detail = 'fail', name + ':' + str(number)
                            result['warnings'].append('secret_suspected')
                            break
                elif check == 'test_floor':
                    for name, before, after, _, _ in versions:
                        old_tests, old_skips = _counts(name, before)
                        new_tests, new_skips = _counts(name, after)
                        if new_tests < old_tests or new_skips > old_skips:
                            status, detail = 'fail', 'test_floor: ' + name
                            break
                else:
                    selected = _commands(commands) if commands is not None else _detect(repo_path, base, deadline)
                    if not selected:
                        record(check, 'not_seen', 'no_commands', started)
                        continue
                    cache = Path(os.environ.get('ORCEXE_STATE_DIR') or Path.home() / '.cache' / 'orcexe') / 'gate'
                    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
                    gate_dir = Path(tempfile.mkdtemp(dir=str(cache))).resolve()
                    checkout = _checkout(repo_path, base, gate_dir, verified, deadline)
                    status, detail = _project(selected, checkout, directory / 'gate',
                                              command_timeout_s, deadline, gate_dir, repo_path, base)
                record(check, status, detail, started)
            except _ConfigError as error:
                record(check, 'fail', 'invalid ' + str(error), started)
            except Exception:
                record(check, 'not_seen', 'observation_unavailable', started)
    except Exception:
        if not result['checks'] or result['checks'][-1]['id'] != current:
            record(current, 'not_seen', 'observation_unavailable', started)
    finally:
        try:
            if temporary is not None:
                temporary.cleanup()
            if gate_dir is not None and os.environ.get('ORCEXE_KEEP_GATE_DIR') != '1':
                shutil.rmtree(gate_dir)
        except Exception:
            unavailable('cleanup_unavailable')
        for check in _IDS[len(result['checks']):]:
            record(check, 'skipped', 'previous_check_incomplete', time.monotonic())
        if all(c['status'] == 'pass' for c in result['checks']):
            result['verdict'] = 'accepted'
            target = classification.targets[0]
            result['diff'].update(target_blob=raw_oids[target], target_path=redact_text(target))
        isolation.annotate(result)
        try:
            validate_gate_result(result)
        except Exception:
            # Invalid/partial observations are never allowed to prevent a fresh receipt.
            result['diff'].update(files=[], added=0, deleted=0)
            result['diff']['head'] = None
            unavailable('observation_unavailable')
        try:
            safe_write_json(Path(run_dir) / 'gate.json', result, mode=0o600)
        except Exception:
            unavailable('receipt_write_unavailable')
            try:
                safe_write_json(Path(run_dir) / 'gate.json', result, mode=0o600)
            except Exception:
                try:
                    (Path(run_dir) / 'gate.json').unlink()
                except Exception:
                    pass
    return result


def validate_gate_result(d) -> None:
    """Reject unknown fields and any verdict unsupported by ordered evidence."""
    _require(isinstance(d, dict) and set(d) == {"schema", "run_id", "verdict", "checks",
             "diff", "escalation", "warnings"}, "keys")
    _require(d["schema"] == "orcexe.gate.v1", "schema")
    _require(isinstance(d["run_id"], str) and _ID.fullmatch(d["run_id"])
             and redact_text(d["run_id"]) == d["run_id"], "run_id")
    _require(isinstance(d["verdict"], str) and d["verdict"] in
             {"accepted", "needs_fix", "escalated", "failed", "unverified"}, "verdict")
    _require(isinstance(d["checks"], list) and len(d["checks"]) == len(_IDS), "checks")
    blocked, failure, missing = False, None, False
    for expected, check in zip(_IDS, d["checks"]):
        _require(isinstance(check, dict) and set(check) == {"id", "status", "detail", "duration_ms"}, "check")
        _require(check["id"] == expected, "check.id")
        status = check["status"]
        _require(isinstance(status, str) and status in {"pass", "fail", "not_seen", "skipped"}, "check.status")
        _require(isinstance(check["detail"], str) and len(check["detail"]) <= 300
                 and redact_text(check["detail"]) == check["detail"], "check.detail")
        _require(type(check["duration_ms"]) is int and check["duration_ms"] >= 0, "check.duration_ms")
        _require((status == "skipped") == blocked, "check.order")
        if status == "fail":
            failure, blocked = expected, True
        elif status == "not_seen":
            missing, blocked = True, True
    verdict = _FAIL[failure] if failure else "unverified" if missing else "accepted"
    _require(d["verdict"] == verdict, "verdict.evidence")
    _require(d["escalation"] == ("lane" if verdict == "escalated" else None), "escalation")
    _require(isinstance(d["warnings"], list) and all(isinstance(x, str) and
             redact_text(x) == x for x in d["warnings"]), "warnings")
    diff = d["diff"]
    _require(isinstance(diff, dict) and {"files", "added", "deleted", "base_commit", "head"} <= set(diff)
             <= {"files", "added", "deleted", "base_commit", "head", "target_blob", "target_path"}, "diff")
    for key in ("target_blob", "target_path"):
        if key in diff or verdict == "accepted":
            value = diff.get(key)
            _require(isinstance(value, str) and
                     (_SHA.fullmatch(value) if key == "target_blob" else
                      _path(value) and redact_text(value) == value), "diff." + key)
    for key in ("base_commit", "head"):
        _require(diff[key] is None or isinstance(diff[key], str) and _SHA.fullmatch(diff[key]), "diff." + key)
    _require(isinstance(diff["files"], list), "diff.files")
    for file in diff["files"]:
        _require(isinstance(file, dict) and set(file) == {"path", "old_path", "status", "old_mode",
                 "new_mode", "binary", "symlink", "submodule", "added", "deleted"}, "diff.file")
        for key in ("path", "old_path"):
            _require(key == "old_path" and file[key] is None or
                     _path(file[key]) and redact_text(file[key]) == file[key], "diff.file." + key)
        _require(isinstance(file["status"], str) and re.fullmatch(r"[MADT]|[RC]\d{1,3}", file["status"]), "diff.status")
        for key in ("old_mode", "new_mode"):
            _require(isinstance(file[key], str) and re.fullmatch(r"[0-7]{6}", file[key]), "diff.mode")
        for key in ("binary", "symlink", "submodule"):
            _require(type(file[key]) is bool, "diff." + key)
        for key in ("added", "deleted"):
            _require(type(file[key]) is int and file[key] >= 0, "diff." + key)
    for key in ("added", "deleted"):
        _require(type(diff[key]) is int and diff[key] >= 0 and
                 diff[key] == sum(f[key] for f in diff["files"]), "diff." + key)
    if verdict == "accepted":
        _require(bool(diff["files"]) and diff["base_commit"] is not None and diff["head"] is not None
                 and diff["added"] + diff["deleted"] > 0,
                 "accepted.diff")
        _require({f["path"] for f in diff["files"]} == {diff["target_path"]}, "accepted.target_path")
        _require(not {"empty_diff", "secret_suspected"}.intersection(d["warnings"]), "accepted.warnings")
        _require(all(f["status"] == "M" and f["old_mode"] == f["new_mode"]
                     and f["old_mode"] in {"100644", "100755"} and f["old_path"] is None
                     and not any(f[k] for k in ("binary", "symlink", "submodule"))
                     for f in diff["files"]), "accepted.scope")
