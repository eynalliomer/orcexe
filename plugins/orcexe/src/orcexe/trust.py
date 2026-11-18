"""Observe and clean only recorded, abandoned Codex/Cursor trust side effects.

Observers retain decoded project keys only. Cleanup scans one bounded line at a
time, then streams bytes to a private backup and atomic replacement; unrelated
configuration is never parsed, logged, or included in the ledger/report.
"""

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import stat
import tempfile
import time

from orcexe import fsio


LIMIT = 8 * 1024 * 1024
SCHEMA = "orcexe.trust-ledger.v1"
_HEADER = re.compile(rb'^\[projects\."((?:[^"\\\x00-\x1f]|\\.)+)"\]\s*$')
_TABLE = re.compile(rb'^\s*\[')
_TRUSTED = re.compile(rb'^\s*trust_level\s*=\s*"trusted"\s*(?:#.*)?$')
_FIELDS = {"id", "tool", "file", "table", "key", "observed_at", "run_id",
           "origin", "state", "removed_at", "backup"}


class TrustError(ValueError):
    """An incomplete observation or unsafe cleanup; never includes config text."""


def _utc():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def config_path(codex_home=None):
    """Resolve HOME/CODEX_HOME at call time, never during module import."""
    home = codex_home if codex_home is not None else os.environ.get("CODEX_HOME") or "~/.codex"
    return Path(home).expanduser().absolute() / "config.toml"


def projects_path(cursor_config_dir=None):
    home = cursor_config_dir if cursor_config_dir is not None else os.environ.get("CURSOR_CONFIG_DIR") or "~/.cursor"
    return Path(home).expanduser().absolute() / "projects"


def project_slug(workspace):
    # Inferred from 2 real samples (2026-11-18); spaces/unicode unverified.
    return re.sub(r"[^A-Za-z0-9]+", "-", str(Path(workspace).absolute())).strip("-")


def _no_symlink(path):
    if path.is_symlink():
        raise TrustError("Refusing symlink in Cursor project directories")


def project_names(path=None):
    """Observe immediate directory names only; never inspect private contents."""
    path = projects_path() if path is None else Path(path)
    _no_symlink(path.parent)
    _no_symlink(path)
    try:
        with os.scandir(path) as children:
            return frozenset(child.name for child in children
                             if child.name != ".orcexe-removed" and
                             (child.is_symlink() or child.is_dir(follow_symlinks=False)))
    except FileNotFoundError:
        return frozenset()


def _display(path):
    path = Path(path).expanduser().absolute()
    try:
        return "~/" + path.relative_to(Path.home()).as_posix()
    except ValueError:
        return str(path)


def _lines(handle):
    """Bound even a single unterminated line and files growing during a scan."""
    total = 0
    while True:
        line = handle.readline(LIMIT - total + 1)
        if not line:
            return
        total += len(line)
        if total > LIMIT:
            raise TrustError("Codex config exceeds 8 MiB")
        yield line


def _key(line):
    match = _HEADER.fullmatch(line)
    if match is None:
        return None
    # TOML basic-string escapes (not JSON: TOML also permits \Uxxxxxxxx).
    raw = match[1].decode("utf-8")
    out, i = [], 0
    escapes = {'"': '"', '\\': '\\', 'b': '\b', 't': '\t', 'n': '\n', 'f': '\f', 'r': '\r'}
    while i < len(raw):
        char = raw[i]
        i += 1
        if char != '\\':
            out.append(char)
            continue
        char = raw[i]
        i += 1
        if char in escapes:
            out.append(escapes[char])
        elif char in ("u", "U"):
            count = 4 if char == "u" else 8
            digits = raw[i:i + count]
            if len(digits) != count or not re.fullmatch('[0-9a-fA-F]+', digits):
                raise TrustError("Invalid project key escape")
            value = int(digits, 16)
            if value > 0x10ffff or 0xd800 <= value <= 0xdfff:
                raise TrustError("Invalid project key escape")
            out.append(chr(value))
            i += count
        else:
            raise TrustError("Invalid project key escape")
    return "".join(out)


def headers(codex_home=None):
    """Return only decoded, exactly matching project table keys."""
    keys = set()
    try:
        with config_path(codex_home).open("rb") as handle:
            for line in _lines(handle):
                key = _key(line)
                if key is not None:
                    keys.add(key)
    except FileNotFoundError:
        pass
    except UnicodeError:
        raise TrustError("Invalid UTF-8 project header") from None
    return frozenset(keys)


def snapshot(codex_home=None):
    """Take a header-only observation for an executor boundary."""
    return headers(codex_home)


def _timestamp(value):
    if not isinstance(value, str):
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).utcoffset().total_seconds() == 0
    except (ValueError, AttributeError, OverflowError):
        return False


def validate(data):
    """Validate the closed ledger schema before trusting any removal authority."""
    if (not isinstance(data, dict) or set(data) != {"schema", "entries"}
            or data["schema"] != SCHEMA or not isinstance(data["entries"], list)):
        raise TrustError("Invalid trust ledger schema")
    previous, active = 0, set()
    for entry in data["entries"]:
        if (not isinstance(entry, dict) or
                set(entry) != (_FIELDS | {"workspace"} if entry.get("tool") == "cursor" else _FIELDS)):
            raise TrustError("Invalid trust ledger entry fields")
        if any(not isinstance(entry[k], str) or not entry[k] for k in
               ("id", "tool", "file", "table", "key", "observed_at", "run_id", "origin", "state")):
            raise TrustError("Invalid trust ledger entry values")
        identifier = re.fullmatch(r'TL-([0-9]{4,})', entry["id"])
        if (identifier is None or int(identifier[1]) <= previous
                or (entry["tool"], entry["table"]) not in (("codex", "projects"), ("cursor", "projects-dir"))
                or entry["origin"] != "orcexe-run"
                or entry["state"] not in ("present", "missing", "removed")
                or not _timestamp(entry["observed_at"])):
            raise TrustError("Invalid trust ledger entry values")
        if entry["tool"] == "cursor":
            key, workspace = entry["key"], entry["workspace"]
            if (key in (".", "..", ".orcexe-removed") or "/" in key or "\\" in key or
                    (workspace is not None and (not isinstance(workspace, str) or
                     not Path(workspace).is_absolute() or project_slug(workspace) != key))):
                raise TrustError("Invalid Cursor project directory")
        previous = int(identifier[1])
        if entry["state"] == "removed":
            if not _timestamp(entry["removed_at"]) or not isinstance(entry["backup"], str) or not entry["backup"]:
                raise TrustError("Invalid trust ledger removal")
        elif entry["removed_at"] is not None or entry["backup"] is not None:
            raise TrustError("Invalid trust ledger removal")
        else:
            identity = (entry["file"], entry["key"])
            if identity in active:
                raise TrustError("Duplicate active trust entry")
            active.add(identity)
    return data


def _load(path):
    return validate(fsio.read_json(path, default={"schema": SCHEMA, "entries": []}))


def record(ledger_path, before, after, run_id, config_path, *, tool="codex", workspaces=()):
    """Append each newly observed key once; pre-existing keys confer no authority."""
    if not after - before:
        return []
    ledger_path = Path(ledger_path)
    fsio._make_parents(ledger_path.parent, 0o700)
    lock = ledger_path.with_name(ledger_path.name + ".lock")
    deadline = time.monotonic() + 10
    while True:
        try:
            fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TrustError("Trust ledger is locked")
            time.sleep(.01)
    try:
        return _record(ledger_path, before, after, run_id, config_path, tool=tool, workspaces=workspaces)
    finally:
        os.close(fd)
        lock.unlink()


def _record(ledger_path, before, after, run_id, config_path, *, tool, workspaces):
    data = _load(ledger_path)
    file = _display(config_path)
    active = {(e["file"], e["key"]) for e in data["entries"] if e["state"] != "removed"}
    number = int(data["entries"][-1]["id"][3:]) if data["entries"] else 0
    added = []
    for key in sorted(after - before):
        if (file, key) in active:
            continue
        number += 1
        entry = dict(id="TL-{:04d}".format(number), tool=tool, file=file,
                     table="projects-dir" if tool == "cursor" else "projects",
                     key=key, observed_at=_utc(), run_id=run_id, origin="orcexe-run",
                     state="present", removed_at=None, backup=None)
        if tool == "cursor":
            matches = {str(Path(p).absolute()) for p in workspaces if project_slug(p) == key}
            entry["workspace"] = matches.pop() if len(matches) == 1 else None
        data["entries"].append(entry)
        added.append(entry)
    if added:
        fsio.atomic_write_json(ledger_path, validate(data), mode=0o600)
    return added


def _refresh(data, path, keys):
    for entry in data["entries"]:
        if entry["state"] != "removed" and Path(entry["file"]).expanduser().absolute() == path:
            entry["state"] = "present" if entry["key"] in keys else "missing"
    return data["entries"]


def list_entries(ledger_path, codex_home=None, *, cursor_config_dir=None):
    """Refresh live states in the returned copy, without writing on a read/dry run."""
    data = _load(ledger_path)
    _refresh(data, config_path(codex_home), headers(codex_home))
    return _refresh(data, projects_path(cursor_config_dir), project_names(projects_path(cursor_config_dir)))


def _scan(path):
    """Collect only header keys, byte ranges, edit flags, and a whole-file digest."""
    digest, sections, offset, current = hashlib.sha256(), [], 0, None
    try:
        with path.open("rb") as handle:
            mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
            for line in _lines(handle):
                digest.update(line)
                if _TABLE.match(line):
                    if current is not None:
                        current["end"] = offset
                    key = _key(line)
                    current = dict(key=key, start=offset, end=None, edited=False) if key is not None else None
                    if current is not None:
                        sections.append(current)
                elif current is not None and line.strip() and not line.lstrip().startswith(b"#"):
                    if not _TRUSTED.fullmatch(line.rstrip(b"\r\n")):
                        current["edited"] = True
                offset += len(line)
    except FileNotFoundError:
        return [], None, None
    except UnicodeError:
        raise TrustError("Invalid UTF-8 project header") from None
    if current is not None:
        current["end"] = offset
    return sections, digest.hexdigest(), mode


def _digest(path):
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for line in _lines(handle):
                digest.update(line)
    except FileNotFoundError:
        return None
    return digest.hexdigest()


def _replace(path, sections, expected, mode):
    """Stream a private backup and filtered replacement, then check and publish."""
    if _digest(path) != expected:
        raise TrustError("Codex config changed during clean")
    backup = path.with_name(path.name + ".orcexe.bak")
    while True:
        try:
            backup_fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            backup = path.with_name(path.name + ".orcexe.bak." + _utc().replace(":", ""))
    temporary = None
    try:
        with os.fdopen(backup_fd, "wb") as saved:
            os.chmod(backup, 0o600)
            fd, temporary = tempfile.mkstemp(prefix=".config.toml-orcexe-", dir=str(path.parent))
            with os.fdopen(fd, "wb") as target, path.open("rb") as source:
                digest, offset, index = hashlib.sha256(), 0, 0
                for line in _lines(source):
                    digest.update(line)
                    saved.write(line)
                    while index < len(sections) and offset >= sections[index]["end"]:
                        index += 1
                    if index == len(sections) or offset < sections[index]["start"]:
                        target.write(line)
                    offset += len(line)
                target.flush()
                os.fsync(target.fileno())
                os.chmod(temporary, mode)
            saved.flush()
            os.fsync(saved.fileno())
        if digest.hexdigest() != expected or _digest(path) != expected:
            raise TrustError("Codex config changed during clean")
        os.replace(temporary, path)
        return _display(backup)
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def clean(ledger_path, codex_home=None, *, cursor_config_dir=None, apply=False, confirm=lambda keys: False):
    """Return candidates/kept/edited/removed keys; apply requires one confirmation."""
    data = _load(ledger_path)
    path = config_path(codex_home)
    sections, digest, mode = _scan(path)
    entries = _refresh(data, path, {s["key"] for s in sections})
    projects = projects_path(cursor_config_dir)
    _refresh(data, projects, project_names(projects))
    report = dict(candidates=[], kept_live=[], skipped_edited=[], skipped_unknown=[], removed=[], backup=None,
                  confirmed=None, dry_run=not apply)
    selected = []
    cursor_selected = []
    for entry in entries:
        if (entry["tool"] == "cursor" and entry["state"] == "present"
                and Path(entry["file"]).expanduser().absolute() == projects):
            key = entry["key"]
            _no_symlink(projects / key)
            if entry["workspace"] is None:
                report["skipped_unknown"].append(key)
            elif os.path.lexists(entry["workspace"]):
                report["kept_live"].append(key)
            else:
                report["candidates"].append(key)
                cursor_selected.append(entry)
            continue
        if entry["state"] != "present" or Path(entry["file"]).expanduser().absolute() != path:
            continue
        key = entry["key"]
        if os.path.lexists(key):
            report["kept_live"].append(key)
            continue
        tables = [s for s in sections if s["key"] == key]
        if any(s["edited"] for s in tables):
            report["skipped_edited"].append(key)
            continue
        report["candidates"].append(key)
        selected.extend(tables)
    if not apply or not (selected or cursor_selected):
        return report
    report["confirmed"] = bool(confirm(list(report["candidates"])))
    if not report["confirmed"]:
        return report
    # A callback may have changed both the config and whether a worktree is live.
    if any(os.path.lexists(e["workspace"] if e["tool"] == "cursor" else e["key"])
           for e in entries if e["state"] == "present" and
           e["key"] in report["candidates"] and
           Path(e["file"]).expanduser().absolute() in (path, projects)):
        raise TrustError("A candidate directory appeared during clean")
    # Recheck every directory after confirmation before making any changes.
    _no_symlink(projects.parent)
    _no_symlink(projects)
    archive = projects / ".orcexe-removed"
    _no_symlink(archive)
    for entry in cursor_selected:
        source = projects / entry["key"]
        _no_symlink(source)
        if not source.is_dir():
            raise TrustError("Cursor project directory changed during clean")
    backup = _replace(path, sorted(selected, key=lambda s: s["start"]), digest, mode) if selected else None
    removed_at = _utc()
    if cursor_selected:
        archive.mkdir(mode=0o700, exist_ok=True)
        destination = Path(tempfile.mkdtemp(prefix=removed_at.replace(":", "") + "-", dir=str(archive)))
        for entry in cursor_selected:
            target = destination / entry["key"]
            os.rename(projects / entry["key"], target)
            entry.update(state="removed", removed_at=removed_at, backup=_display(target))
        if backup is None:
            backup = _display(destination)
    for entry in entries:
        if (entry["tool"] == "codex" and entry["state"] == "present" and entry["key"] in report["candidates"]
                and Path(entry["file"]).expanduser().absolute() == path):
            entry.update(state="removed", removed_at=removed_at, backup=backup)
    fsio.atomic_write_json(ledger_path, validate(data), mode=0o600)
    report.update(removed=list(report["candidates"]), backup=backup)
    return report
