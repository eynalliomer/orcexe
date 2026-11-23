"""Passive project memory, ``orcexe.ledger.v1`` (UTF-8 JSONL).

Data allow-lists (all listed fields are required, including nullable fields):
* task.classified: mode, lane, signals (list of text ids), targets_count (int),
  task_sha256 (64 lowercase hex digits), task_preview (text, at most 200 chars).
* task.dispatched: tool, model, effort, route_basis, branch (text), attempt (int).
* attempt.ended: run_id, status (run.v1 vocabulary), exit_code (int or null), duration_ms
  (nonnegative int or null), usage (input, cached_read, cache_write, output,
  reasoning: nonnegative int or null), warnings (list of text).
* gate.result: run_id, verdict (gate.v1 vocabulary), failed_check (text or null), duration_ms
  (nonnegative int or null).
* task.state: from, to, reason (text).
* lane.escalated: from_lane, to_lane, reason (text).
* question.asked: count (int >= 1), topic (text, at most 80 chars).
* questions.form: count (int >= 0, not bool), form_sha256 (64 lowercase hex).
* orchestrator.usage: input, output (nonnegative int or null), source
  (measured or not_seen).
All text, including dropped field names and list entries, is redacted before
truncation (200 characters unless specified above). Nested unknown fields
have their own _dropped list; only unknown fields are dropped. Missing or invalid
known fields raise ValueError naming the field before the file is opened.
Integers exclude bool and are bounded to 0..2**53-1 (count starts at 1;
exit_code is -255..255). Floats, including NaN/Inf, are invalid. Explicit nulls
are preserved; unavailable measurements never become zero. mode is
existing|new|everyday; lane/from_lane/to_lane are fast|standard|risky; source is
measured|not_seen. task_sha256 must be exactly 64 lowercase hex digits.
Redaction inherits orcexe.redact's limits and deferred B10 list: in particular,
base64-wrapped secrets are not decoded. See that module for the full gap list.

Writers hold an exclusive OS lock across tail lookup, one O_APPEND os.write,
and fsync: flock on POSIX, msvcrt.locking on byte zero on Windows (including
an empty file). Readers hold a shared flock, or the same exclusive Windows
byte lock, until exhausted/closed. A streaming reader validates each record
before yielding it; callers must exhaust it to verify the entire file.
Append reads backwards in 4096-byte blocks, stopping at the preceding newline;
it validates only the last record, not the history.

The chain hashes exact line bytes without LF, not reserialized objects. A chain
alone cannot detect suffix deletion or final-record edits, so a ledger at
``<repo>/.orcexe/ledger.jsonl`` of a Git checkout is anchored (T55): under the
same exclusive lock, every append first requires the external head ref
``refs/orcexe/ledger-head`` (a blob holding ``seq`` and the sha256 of the last
line) to match the current tail, then moves it with compare-and-swap
(``git update-ref <ref> <new> <old>``); a refused swap truncates its line again.
The first anchoring writes a ``ledger.anchored`` record (hidden from ordinary
readers), so a missing ref of a ledger that has one is a deleted ref, never a
fresh adoption. Readers that decide state call ``verify_head``.

Within one command (``session``/``in_session``) the ledger also remembers the last
``(seq, sha256, size)`` it wrote or verified; any other tail seen by a later append,
read or verify of the same command is a ForeignRecord (``state_tampered``) and writes
the ``state-tampered`` marker with that trusted point. ``repair`` quarantines what
follows the marker's point, or the head ref's record, and never anchors unvouched
records without ``accept_unanchored``. Out of scope: a process that, between two
commands, rewrites the ledger and the ref consistently, or empties the ledger and
deletes the ref (a fresh ledger), or deletes a loose ref file to fall back to an
older packed value while also truncating the ledger to it. Links are never followed.
File fsync does not fsync directories.
"""

from contextlib import ExitStack, contextmanager
from functools import wraps
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import statistics
import subprocess
import time

from . import fsio
from .redact import redact_text, sanitize_record


_SCHEMA = "orcexe.ledger.v1"
_ZERO = "0" * 64
_TASK = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HEX = re.compile(r"[0-9a-f]{64}\Z")
_TS = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z\Z")
_MAX_INT = 2**53 - 1
# run/gate expose no public vocabularies; tests assert these copies match.
_STATUSES = frozenset(("completed", "failed", "quota", "auth", "timeout", "stalled",
                       "blocked", "cancelled"))
_VERDICTS = frozenset(("accepted", "needs_fix", "escalated", "failed", "unverified"))
_ENUMS = {
    "mode": frozenset(("existing", "new", "everyday")),
    "lane": frozenset(("fast", "standard", "risky")),
    "status": _STATUSES,
    "verdict": _VERDICTS,
    "source": frozenset(("measured", "not_seen")),
}
_USAGE = dict.fromkeys(("input", "cached_read", "cache_write", "output", "reasoning"), "counter?")
_SHAPES = {
    "task.classified": dict(mode="mode", lane="lane", signals="list", targets_count="int",
                            task_sha256="sha256", task_preview="text"),
    "task.dispatched": dict(tool="text", model="text", effort="text", route_basis="text",
                            branch="text", attempt="int"),
    "attempt.ended": dict(run_id="text", status="status", exit_code="exit_code?",
                          duration_ms="counter?", usage=_USAGE, warnings="list"),
    "gate.result": dict(run_id="text", verdict="verdict", failed_check="text?", duration_ms="counter?"),
    "task.state": {"from": "text", "to": "text", "reason": "text"},
    "lane.escalated": dict(from_lane="lane", to_lane="lane", reason="text"),
    "question.asked": dict(count="positive", topic="topic"),
    "questions.form": dict(count="counter", form_sha256="sha256"),
    "orchestrator.usage": dict(input="counter?", output="counter?", source="source"),
    "ledger.anchored": dict(),
}


class LedgerError(RuntimeError):
    """A safe reason, optionally with a 1-based line number; never file data."""

    def __init__(self, line, reason=None):
        self.line = line if reason is not None else None
        self.reason = reason if reason is not None else line
        super().__init__("line {}: {}".format(line, reason) if reason is not None else line)


class HeadMismatch(LedgerError):
    """The ledger tail differs from the external head ref (or the ref moved)."""

    def __init__(self, detail="ledger_head_mismatch"):
        self.detail = detail
        self.head = self.state = None
        super().__init__("ledger_head_mismatch")

    def found(self, head, state):
        """Record what the ref says and what the ledger ends with (for repair messages)."""
        self.head, self.state = head, state
        return self


class ForeignRecord(LedgerError):
    """A ledger state this command did not write or verify (P2-1): terminal ``state_tampered``."""

    def __init__(self, detail="foreign_record"):
        self.detail = detail
        super().__init__("state_tampered")


HEAD_REF = "refs/orcexe/ledger-head"
_HEAD_SCHEMA = "orcexe.ledger.head.v1"
# Written once into every anchored ledger, so a later missing ref is a deleted ref, not a new ledger.
ANCHORED = "ledger.anchored"
MARKER = "state-tampered"
_MARKER_SCHEMA = "orcexe.tamper.v1"
REPAIR_HINT = ("Repair: run `orcexe ledger verify` to see the problem, then `orcexe ledger repair` "
               "(keeps the trusted prefix, quarantines the rest, re-anchors the head ref).")
TAMPER_HINT = ("Recovery: `orcexe ledger repair` quarantines every record after the last trusted one "
               "the marker names; inspect the quarantine file, `git status` and .orcexe/ (plan.json, "
               "decisions.jsonl, runs/), then remove .orcexe/state-tampered and rerun with --resume.")


def reason(error):
    """Closed diagnostic: a foreign record, a moved/missing head, or anything unreadable."""
    if isinstance(error, ForeignRecord):
        return "state_tampered"
    return "ledger_head_mismatch" if isinstance(error, HeadMismatch) else "ledger_corrupt"


# Per-command trust (P2-1): while a command runs, the last ``(seq, sha256, size)`` this process
# wrote or verified, per ledger path. Any other tail seen during the command is foreign.
_SESSIONS = []


@contextmanager
def session():
    """Scope of one CLI command; nested commands (plan --run -> task) share the outer one."""
    if _SESSIONS:
        yield
        return
    _SESSIONS.append({})
    try:
        yield
    finally:
        _SESSIONS.pop()


def in_session(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        with session():
            return function(*args, **kwargs)
    return wrapped


def _key(path):
    return os.path.abspath(str(path))


def trusted(path):
    """The last ``(seq, sha256, size)`` this command wrote or verified, or None."""
    return _SESSIONS[-1].get(_key(path)) if _SESSIONS else None


def _remember(path, state):
    if _SESSIONS:
        _SESSIONS[-1][_key(path)] = state


def _check(path, state):
    known = trusted(path)
    if known is not None and tuple(known) != tuple(state):
        write_marker(Path(path).parent, known)
        raise ForeignRecord("tail_differs")


def write_marker(directory, trusted_state):
    """Create the persistent tamper marker once, carrying the last trusted ledger state."""
    path = Path(directory) / MARKER
    if os.path.lexists(path):
        return
    point = None if trusted_state is None else dict(seq=trusted_state[0], sha256=trusted_state[1],
                                                     size=trusted_state[2])
    data = (json.dumps(dict(schema=_MARKER_SCHEMA, ledger=point), sort_keys=True) + "\n").encode("ascii")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        return
    except OSError:
        return  # A replaced control directory must not mask the terminal reason.
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


def read_marker(directory):
    """``(present, trusted)``: trusted is ``(seq, sha256, size)`` or None (legacy/unknown point)."""
    path = Path(directory) / MARKER
    if not os.path.lexists(path):
        return False, None
    try:
        if path.is_symlink():
            return True, None
        value = json.loads(path.read_text(encoding="ascii"))
        point = value.get("ledger") if isinstance(value, dict) and value.get("schema") == _MARKER_SCHEMA else None
        if (isinstance(point, dict) and _valid(point.get("seq"), "counter") and _valid(point.get("size"), "counter")
                and isinstance(point.get("sha256"), str) and _HEX.fullmatch(point["sha256"])):
            return True, (point["seq"], point["sha256"], point["size"])
    except (OSError, ValueError, UnicodeError):
        pass
    return True, None


def default_path(main_worktree):
    return Path(main_worktree) / ".orcexe" / "ledger.jsonl"


def _git(repo, *args, data=None):
    """Hardened Git (no user/system config, hooks, prompts or replace objects)."""
    env = {k: os.environ[k] for k in ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT")
           if k in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
               GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_NO_REPLACE_OBJECTS="1",
               LC_ALL="C", LANG="C")
    argv = ["git", "--no-replace-objects", "--no-pager", "-c", "core.hooksPath=" + os.devnull,
            "-c", "core.fsmonitor=false"]
    result = subprocess.run(argv + list(args), cwd=str(repo), env=env, input=data,
                            stdin=None if data is not None else subprocess.DEVNULL,
                            capture_output=True, timeout=10)
    return result.returncode, result.stdout


class _Anchor:
    """The head ref of one repository; values are ``(seq, sha256 of last line)``."""

    def __init__(self, repo):
        self.repo = Path(repo)

    def read(self, strict=True):
        """Return ``(oid, (seq, digest))``, or ``(None, None)`` when no ref exists.

        strict=False returns ``(oid, "unreadable")`` for a ref whose blob is not a head.
        """
        code, output = _git(self.repo, "for-each-ref", "--format=%(refname) %(objectname) %(symref)", HEAD_REF)
        if code:
            raise HeadMismatch("head_unreadable")
        for line in output.decode("utf-8", "replace").splitlines():
            name, oid, symbolic = (line.split(" ", 2) + ["", ""])[:3]
            if name != HEAD_REF:
                continue
            if symbolic:
                raise HeadMismatch("head_symbolic")
            code, blob = _git(self.repo, "cat-file", "blob", oid)
            match = re.fullmatch(_HEAD_SCHEMA.replace(".", r"\.") + r"\nseq ([0-9]+)\nsha256 ([0-9a-f]{64})\n",
                                 blob.decode("utf-8", "replace")) if not code else None
            if match is None:
                if not strict:
                    return oid, "unreadable"
                raise HeadMismatch("head_unreadable")
            return oid, (int(match[1]), match[2])
        return None, None

    def write(self, state, old):
        """Compare-and-swap from ``old`` (None: the ref must not exist yet)."""
        content = "{}\nseq {}\nsha256 {}\n".format(_HEAD_SCHEMA, state[0], state[1]).encode("ascii")
        code, oid = _git(self.repo, "hash-object", "-w", "--stdin", data=content)
        oid = oid.decode("ascii", "replace").strip()
        if code or not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", oid):
            raise HeadMismatch("head_unwritable")
        code, _ = _git(self.repo, "update-ref", "--no-deref", "-m", "orcexe ledger", HEAD_REF, oid,
                       old if old is not None else "0" * len(oid))
        if code:
            # Someone else moved (or created) the ref since it was read: refuse, never overwrite.
            raise HeadMismatch("head_moved")
        return oid


def _anchor_for(path):
    """Anchor only ``<repo>/.orcexe/ledger.jsonl`` of a directory that holds its own ``.git``."""
    path = Path(path)
    if path.name != "ledger.jsonl" or path.parent.name != ".orcexe":
        return None
    repo = path.parent.parent
    return _Anchor(repo) if os.path.lexists(repo / ".git") else None


def _text(value, limit=200):
    return redact_text(value)[:limit]


def _valid(value, spec):
    if isinstance(spec, dict):
        return isinstance(value, dict) and _valid_data(value, spec)
    if spec.endswith("?"):
        return value is None or _valid(value, spec[:-1])
    if spec in ("int", "counter", "positive"):
        return type(value) is int and (1 if spec == "positive" else 0) <= value <= _MAX_INT
    if spec == "exit_code":
        return type(value) is int and -255 <= value <= 255
    if spec == "list":
        return (isinstance(value, list)
                and all(isinstance(item, str) and len(item) <= 200 for item in value))
    if not isinstance(value, str):
        return False
    if spec == "sha256":
        return _HEX.fullmatch(value) is not None
    if spec in _ENUMS:
        return value in _ENUMS[spec]
    return len(value) <= (80 if spec == "topic" else 200)


def _valid_data(data, shape):
    if not shape.keys() <= data.keys():
        return False
    for key, value in data.items():
        if key == "_dropped":
            if not isinstance(value, list) or not all(isinstance(v, str) and len(v) <= 200 for v in value):
                return False
        elif key not in shape or not _valid(value, shape[key]):
            return False
    return True


def _sanitize(data, shape, path="data"):
    """Adapt lists/nulls/strict integers to sanitize_record's scalar schema."""
    for key in shape:
        if key not in data:
            raise ValueError("missing " + path + "." + key)
    scalars, schema, extras = {}, {}, {}
    for key, value in data.items():
        spec = shape.get(key)
        if spec is None:
            scalars[key] = value  # No schema entry: drop without inspecting value.
            continue
        field = path + "." + key
        if isinstance(spec, dict):
            if not isinstance(value, dict):
                raise ValueError("invalid " + field)
            extras[key] = _sanitize(value, spec, field)
        elif spec.endswith("?") and value is None:
            extras[key] = None
        elif spec == "list":
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise ValueError("invalid " + field)
            # sanitize_record also handles JSON embedded in a string.
            extras[key] = [_text(sanitize_record({"v": v}, {"v": "str"})["v"]) for v in value]
        elif spec.rstrip("?") in ("text", "topic"):
            if not isinstance(value, str):
                raise ValueError("invalid " + field)
            scalars[key], schema[key] = value, "str"
        elif _valid(value, spec):
            scalars[key], schema[key] = value, "str" if isinstance(value, str) else "num"
        else:
            raise ValueError("invalid " + field)
    result = sanitize_record(scalars, schema)
    dropped = result.pop("_dropped", [])
    for key in list(result):
        if isinstance(result[key], str):
            result[key] = _text(result[key], 80 if shape[key] == "topic" else 200)
        if not _valid(result[key], shape[key]):
            raise ValueError("invalid " + path + "." + key)
    result.update(extras)
    if dropped:
        result["_dropped"] = sorted(_text(str(key)) for key in dropped)
    return result


def _serialize(record):
    line = json.dumps(record, sort_keys=True, ensure_ascii=False, allow_nan=False)
    for char in ("\u2028", "\u2029", "\u0085", "\x0c"):
        line = line.replace(char, "\\u{:04x}".format(ord(char)))
    if "\n" in line or "\r" in line:
        raise ValueError("JSONL serialization contains a raw newline")
    return line.encode("utf-8")


@contextmanager
def _lock(fd, exclusive=True):
    if os.name == "nt":
        import msvcrt
        os.lseek(fd, 0, os.SEEK_SET)
        while True:
            try:
                msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                break
            except OSError as error:
                if error.errno not in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise
                time.sleep(.01)
        try:
            yield
        finally:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)


def _decode(line, number):
    try:
        record = json.loads(line.decode("utf-8"))
    except (ValueError, UnicodeError, RecursionError):
        raise LedgerError(number, "invalid_json") from None
    if not isinstance(record, dict) or set(record) != {"schema", "seq", "ts", "event", "task", "data", "prev"}:
        raise LedgerError(number, "schema")
    if record["schema"] != _SCHEMA:
        raise LedgerError(number, "schema")
    if not _valid(record["seq"], "positive"):
        raise LedgerError(number, "seq")
    if not isinstance(record["event"], str) or record["event"] not in _SHAPES:
        raise LedgerError(number, "event")
    if not isinstance(record["task"], str) or not _TASK.fullmatch(record["task"]):
        raise LedgerError(number, "task")
    if not isinstance(record["prev"], str) or not _HEX.fullmatch(record["prev"]):
        raise LedgerError(number, "prev")
    stamp = record["ts"]
    if not isinstance(stamp, str) or not _TS.fullmatch(stamp):
        raise LedgerError(number, "ts")
    try:
        datetime.fromisoformat(stamp[:-1])
    except ValueError:
        raise LedgerError(number, "ts") from None
    if not isinstance(record["data"], dict) or not _valid_data(record["data"], _SHAPES[record["event"]]):
        raise LedgerError(number, "data")
    return record


def _tail(fd):
    size = os.lseek(fd, 0, os.SEEK_END)
    if size == 0:
        return None, _ZERO
    os.lseek(fd, size - 1, os.SEEK_SET)
    if os.read(fd, 1) != b"\n":
        raise LedgerError("truncated_tail")
    end, chunks = size - 1, []
    while end:
        start = max(0, end - 4096)
        os.lseek(fd, start, os.SEEK_SET)
        chunk = os.read(fd, end - start)
        boundary = chunk.rfind(b"\n")
        chunks.append(chunk[boundary + 1:])
        if boundary >= 0:
            break
        end = start
    line = b"".join(reversed(chunks))
    try:
        record = _decode(line, 1)
    except LedgerError:
        raise LedgerError("truncated_tail") from None
    return record, hashlib.sha256(line).hexdigest()


def _lines(handle):
    """Yield ``(line_bytes, record, digest)`` per valid line in order; raise at the first bad one."""
    previous, stamp = _ZERO, ""
    for number, line in enumerate(handle, 1):
        if not line.endswith(b"\n"):
            raise LedgerError(number, "truncated_tail")
        raw = line[:-1]
        record = _decode(raw, number)
        if record["seq"] != number:
            raise LedgerError(number, "seq")
        if record["prev"] != previous:
            raise LedgerError(number, "prev")
        if record["ts"] < stamp:
            raise LedgerError(number, "ts_regression")
        previous, stamp = hashlib.sha256(raw).hexdigest(), record["ts"]
        yield line, record, previous


def _scan(fd):
    """Whole-chain state of an open, locked descriptor: ``(seq, digest, size, anchored, last)``."""
    os.lseek(fd, 0, os.SEEK_SET)
    seq, digest, size, anchored, last = 0, _ZERO, 0, False, None
    with os.fdopen(fd, "rb", closefd=False) as handle:
        for line, record, digest in _lines(handle):
            seq, size, last = record["seq"], size + len(line), record
            anchored = anchored or record["event"] == ANCHORED
    return seq, digest, size, anchored, last


def _state_text(state):
    return "seq {} (sha256 {})".format(state[0], state[1][:12]) if isinstance(state, (tuple, list)) else "no head ref"


def describe(record):
    """One safe line per record for repair output: seq, task, event and its target state."""
    data = record["data"]
    extra = "".join(" " + key + "=" + str(data[key]) for key in ("to", "reason", "run_id")
                    if isinstance(data.get(key), str))
    return "seq {} {} {} {}{}".format(record["seq"], record["ts"], record["task"], record["event"], extra)


class RepairRefused(LedgerError):
    def __init__(self, detail, report):
        self.detail, self.report = detail, report
        super().__init__("repair_refused")


class Ledger:
    def __init__(self, path, *, clock=None, anchor=True):
        """clock() returns an aware datetime; UTC milliseconds are recorded.

        anchor=False disables the head ref even for ``<repo>/.orcexe/ledger.jsonl``.
        """
        self.path = Path(path)
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)
        self.anchored = anchor

    def _anchor(self):
        return _anchor_for(self.path) if self.anchored else None

    def _open(self, flags):
        """Never follow a linked ledger (P3-3); only a regular file is a ledger."""
        try:
            fd = os.open(self.path, flags | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
        except FileNotFoundError:
            raise
        except OSError as error:
            if error.errno in (errno.ELOOP, getattr(errno, "EMLINK", -1), getattr(errno, "EFTYPE", -1)):
                raise LedgerError("ledger_symlink") from None
            raise
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise LedgerError("ledger_not_regular")
        return fd

    def _line(self, event, task, clean, last, previous):
        now = self.clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise ValueError("ledger clock must return an aware datetime")
        stamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        if last and stamp < last["ts"]:
            raise LedgerError("clock_regression")
        if last and last["seq"] == _MAX_INT:
            raise ValueError("invalid seq: integer bound exceeded")
        record = dict(schema=_SCHEMA, seq=last["seq"] + 1 if last else 1,
                      ts=stamp, event=event, task=task, data=clean, prev=previous)
        line = _serialize(record) + b"\n"
        return record, line, hashlib.sha256(line[:-1]).hexdigest()

    def _write(self, fd, lines, size):
        data = b"".join(lines)
        if os.write(fd, data) != len(data):
            raise LedgerError("short_write")  # A torn line stays visible (truncated_tail), never repaired here.
        os.fsync(fd)

    def append(self, event, task, data):
        if not isinstance(event, str) or event not in _SHAPES or event == ANCHORED:
            raise ValueError("unknown ledger event")
        if not isinstance(task, str) or not _TASK.fullmatch(task) or redact_text(task) != task:
            raise ValueError("invalid task id")
        if not isinstance(data, dict):
            raise TypeError("ledger data must be a dict")
        clean = _sanitize(data, _SHAPES[event])
        fsio._make_parents(self.path.parent, 0o700)
        fd = self._open(os.O_RDWR | os.O_CREAT | os.O_APPEND)
        try:
            with _lock(fd):
                size = os.lseek(fd, 0, os.SEEK_END)
                last, previous = _tail(fd)
                # P2-1: during a command only the tail this process wrote or verified is ours.
                _check(self.path, (last["seq"] if last else 0, previous, size))
                anchor, old, lines = self._anchor(), None, []
                if anchor is not None:
                    old, head = anchor.read()
                    if head is None:
                        anchored = False
                        if last is not None:
                            # Adopt an unanchored ledger only after its whole chain verifies, and
                            # only once: a ledger that was ever anchored has lost its ref (P3-1).
                            anchored = _scan(fd)[3]
                        if anchored:
                            raise HeadMismatch("ref_deleted")
                        last, line, previous = self._line(ANCHORED, "ledger", {}, last, previous)
                        lines.append(line)
                    elif head != (last["seq"] if last else 0, previous):
                        raise HeadMismatch("tail_differs")
                record, line, digest = self._line(event, task, clean, last, previous)
                lines.append(line)
                self._write(fd, lines, size)
                if anchor is not None:
                    try:
                        # Compare-and-swap from the value read under this lock.
                        anchor.write((record["seq"], digest), old)
                    except HeadMismatch:
                        os.ftruncate(fd, size)  # P3-4: a refused append leaves nothing behind.
                        os.fsync(fd)
                        raise
                _remember(self.path, (record["seq"], digest, size + sum(len(x) for x in lines)))
                return record
        finally:
            os.close(fd)

    def records(self, *, task=None, event=None, include_meta=False):
        """Yield matching records, checking even filtered-out lines in order.

        During a command the tail must still be the one this process wrote or verified.
        """
        try:
            fd = self._open(os.O_RDWR if os.name == "nt" else os.O_RDONLY)
        except FileNotFoundError:
            _check(self.path, (0, _ZERO, 0))
            return
        try:
            with _lock(fd, exclusive=False):
                if trusted(self.path) is not None:
                    size = os.lseek(fd, 0, os.SEEK_END)
                    last, previous = _tail(fd)
                    _check(self.path, (last["seq"] if last else 0, previous, size))
                    os.lseek(fd, 0, os.SEEK_SET)
                # Keep the locked descriptor open until the lock is released.
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    for _, record, _ in _lines(handle):
                        if record["event"] == ANCHORED and not include_meta:
                            continue
                        if (task is None or record["task"] == task) and (event is None or record["event"] == event):
                            yield record
        finally:
            os.close(fd)

    def verify(self):
        count = sum(1 for _ in self.records(include_meta=True))
        return {"records": count, "last_seq": count, "ok": True}

    def verify_head(self):
        """Verify the whole chain and that the head ref names its tail (readers deciding state).

        A never-anchored ledger with records is adopted once: a ``ledger.anchored`` record is
        appended and the ref created by compare-and-swap. A missing ref of a ledger that was
        anchored before is a deleted ref. Raises HeadMismatch, ForeignRecord or LedgerError.
        """
        result = dict(records=0, last_seq=0, ok=True, anchored=False, adopted=False)
        anchor = self._anchor()
        try:
            fd = self._open(os.O_RDWR)
        except FileNotFoundError:
            _check(self.path, (0, _ZERO, 0))
            if anchor is not None:
                _, head = anchor.read()
                if head is not None and head != (0, _ZERO):
                    raise HeadMismatch("ledger_missing").found(head, (0, _ZERO))
                result["anchored"] = head is not None
            _remember(self.path, (0, _ZERO, 0))
            return result
        try:
            with _lock(fd):
                seq, digest, size, anchored, last = _scan(fd)
                if os.lseek(fd, 0, os.SEEK_END) != size:
                    raise LedgerError("truncated_tail")
                _check(self.path, (seq, digest, size))
                result.update(records=seq, last_seq=seq)
                if anchor is not None:
                    old, head = anchor.read()
                    if head is None and anchored:
                        raise HeadMismatch("ref_deleted").found(None, (seq, digest))
                    if head is None and seq:
                        record, line, digest = self._line(ANCHORED, "ledger", {}, last, digest)
                        self._write(fd, [line], size)
                        try:
                            anchor.write((record["seq"], digest), None)
                        except HeadMismatch:
                            os.ftruncate(fd, size)
                            os.fsync(fd)
                            raise
                        seq, size = record["seq"], size + len(line)
                        result.update(records=seq, last_seq=seq, adopted=True)
                    elif head is not None and head != (seq, digest):
                        raise HeadMismatch("tail_differs").found(head, (seq, digest))
                    result["anchored"] = head is not None or result["adopted"]
                _remember(self.path, (seq, digest, size))
                return result
        finally:
            os.close(fd)

    def repair(self, *, now=None, accept_unanchored=False):
        """Keep the trusted prefix, quarantine the rest byte-for-byte, re-anchor the head ref.

        The trusted prefix ends at the record a ``state-tampered`` marker names; else at the
        head ref when it names a prefix record (later records are quarantined, never silently
        anchored); else at the longest valid prefix of a never-anchored ledger. A head ref that
        names no record of the ledger, a deleted ref of an anchored ledger, or a marker without
        a trusted point with unanchored records is refused unless ``accept_unanchored``.
        Returns the report; raises RepairRefused carrying it.
        """
        now = now or datetime.now(timezone.utc)
        report = dict(moved=None, quarantined=[], head_found=None, head_now=None, reanchored=False,
                      records=0, marker=None, basis=None, refused=None)
        with ExitStack() as stack:
            try:
                fd = self._open(os.O_RDWR)
            except FileNotFoundError:
                fd = None
            if fd is not None:
                stack.callback(os.close, fd)
                stack.enter_context(_lock(fd))
            entries, failure, size = [], None, 0
            if fd is not None:
                size, end = os.lseek(fd, 0, os.SEEK_END), 0
                os.lseek(fd, 0, os.SEEK_SET)
                with os.fdopen(fd, "rb", closefd=False) as handle:
                    lines = _lines(handle)
                    while True:
                        try:
                            line, record, digest = next(lines)
                        except StopIteration:
                            break
                        except LedgerError as error:
                            failure = error
                            break
                        end += len(line)
                        entries.append(dict(end=end, sha=digest, record=record))
            present, point = read_marker(self.path.parent)
            report["marker"] = (dict(seq=point[0], sha256=point[1]) if point else "unknown") if present else None
            anchor = self._anchor()
            old, head = anchor.read(strict=False) if anchor is not None else (None, None)
            report["head_found"] = head
            ends = [(0, _ZERO)] + [(e["record"]["seq"], e["sha"]) for e in entries]

            def prefix(state):
                return isinstance(state, tuple) and 0 <= state[0] < len(ends) and ends[state[0]] == tuple(state[:2])

            ever_anchored = any(e["record"]["event"] == ANCHORED for e in entries)
            keep, refused = len(entries), None
            if point is not None:
                keep, report["basis"] = point[0], "marker"
                if not prefix(point[:2]):
                    refused = "the marker's last trusted record is not in this ledger"
            elif anchor is not None and prefix(head):
                keep, report["basis"] = (len(entries), "accepted") if accept_unanchored else (head[0], "head_ref")
            elif anchor is not None and (head is not None or ever_anchored):
                report["basis"] = "accepted"
                refused = ("the head ref names no record of this ledger" if head is not None
                           else "the head ref of an anchored ledger was deleted")
            else:
                report["basis"] = "valid_prefix"
                if present and anchor is not None and entries:
                    refused = "a tamper marker without a trusted point; nothing proves these records"
            if refused and accept_unanchored and point is None:
                refused = None
            report["quarantined"] = [describe(e["record"]) for e in entries[keep:]]
            report["records"] = keep
            if refused:
                report["refused"] = refused
                raise RepairRefused(refused, report)
            cut = entries[keep - 1]["end"] if keep else 0
            state = ends[keep]
            if fd is not None and cut < size:
                os.lseek(fd, cut, os.SEEK_SET)
                data = b""
                while len(data) < size - cut:
                    chunk = os.read(fd, size - cut - len(data))
                    if not chunk:
                        break
                    data += chunk
                if len(data) != size - cut:
                    raise LedgerError("short_read")
                target = self._quarantine(data, now)
                os.ftruncate(fd, cut)
                os.fsync(fd)
                report["moved"] = dict(path=str(target), bytes=len(data),
                                       lines=data.count(b"\n") + (0 if data.endswith(b"\n") else 1),
                                       first_line=keep + 1,
                                       reason=(failure.reason if failure is not None else "untrusted")
                                       if keep == len(entries) or failure is None else "untrusted")
                size = cut
            if anchor is not None:
                if keep and not any(e["record"]["event"] == ANCHORED for e in entries[:keep]):
                    # Record the anchoring itself, so a later deleted ref is detected (P3-1).
                    record, line, digest = self._line(ANCHORED, "ledger", {}, entries[keep - 1]["record"],
                                                      entries[keep - 1]["sha"])
                    self._write(fd, [line], size)
                    state = (record["seq"], digest)
                if head != state:
                    anchor.write(state, old)
                    report["reanchored"] = True
            report["head_now"] = state
            return report

    def _quarantine(self, data, now):
        stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        for index in range(1, 1000):
            target = self.path.parent / ("ledger.quarantine-" + stamp + ("" if index == 1 else "-" + str(index)) + ".jsonl")
            try:
                out = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                              | getattr(os, "O_BINARY", 0), 0o600)
            except FileExistsError:
                continue
            try:
                if os.write(out, data) != len(data):
                    raise LedgerError("short_write")
                os.fsync(out)
            finally:
                os.close(out)
            os.chmod(target, 0o600)
            return target
        raise LedgerError("quarantine_unavailable")


def _metric(value=None, n=0):
    return {"value": value, "status": "measured" if value is not None else "not_seen", "n": n}


def _task_key(task, data, event, run_starts):
    if event == "attempt.ended":
        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id in run_starts:
            return run_starts[run_id]
        return (None, task)
    plan_id = data.get("plan_id")
    return (plan_id if isinstance(plan_id, str) else None, task)


def kpis(ledger, task=None):
    """Aggregate a verified stream, optionally restricted to one task id.

    Task key is ``(plan_id, task)``: ``plan_id`` is ``data["plan_id"]`` when the
    record has it, else ``None``. ``attempt.ended`` has no ``plan_id``: attribute
    it to the key of the ``attempt.started`` with the same ``run_id``; if none,
    ``(None, task)``. ``kpis(ledger, task=X)`` filters ledger rows by task id; two
    plans' ``X`` remain two keys.

    **dispatched** — keys with ``task.dispatched`` or ``attempt.started``.

    **gate_pass_rate** (K7) — (``gate.result`` with verdict ``accepted`` +
    ``gate.passed``) / (``gate.result`` + ``gate.passed`` + ``gate.failed``). One
    gate evaluation each; n is the denominator count.

    **task_acceptance_rate** (K7) — dispatched keys whose last ``task.state.to``
    is ``accepted`` / dispatched keys. Cancelled, ``rejected``, ``exhausted``,
    ``gate_passed``, ``failed`` and keys with no ``task.state`` stay in the
    denominator. Never-dispatched keys (e.g. ``blocked``) are excluded.

    **rounds_per_task** — median ``attempt.ended`` count over classified ∪
    dispatched ∪ keys with ``attempt.ended`` (zero for classified without
    attempts); n is that population.

    **lane_escalation_rate** — keys in (classified ∪ dispatched) with
    ``lane.escalated`` / that population.

    **l1_calls** — count of ``l1.started`` records; n is distinct keys with
    ``l1.started``. Measured when at least one key is dispatched (0 is a
    measurement), else ``not_seen``.

    **questions_per_plan** — mean ``count`` over distinct ``form_sha256`` of
    ``questions.form`` (repeated hash counts once with its latest count; a
    zero-question form is a measurement of 0); n is distinct forms; ``not_seen``
    when there is none.

    **questions_per_task**, **orchestration_overhead**, **time_to_first_dispatch_ms**
    — unchanged semantics. Overhead n is executor attempts; dispatch latency is
    the median of first ``task.dispatched`` minus first ``task.classified`` per
    key with both (milliseconds).

    A rate with numerator 0 is ``0.0`` ``measured``; an empty population is
    ``not_seen``, never 0. Plan events besides those above are ignored.
    Unobserved token samples produce ``not_seen`` for overhead.
    """
    classified, dispatched, questions, attempts, escalated = set(), set(), {}, {}, set()
    first_classified, first_dispatched = {}, {}
    unknown_questions = set()
    run_starts, last_state = {}, {}
    l1_keys, l1_records = set(), 0
    plan_forms = {}
    gate_pass = gate_total = 0
    executor = orchestrator = samples = attempt_count = 0
    complete = True
    for record in ledger.records(task=task):
        task_id, event, data = record["task"], record["event"], record["data"]
        key = _task_key(task_id, data, event, run_starts)
        if event == "task.classified":
            classified.add(key)
            first_classified.setdefault(key, record["ts"])
        elif event == "task.dispatched":
            dispatched.add(key)
            first_dispatched.setdefault(key, record["ts"])
        elif event == "attempt.started":
            dispatched.add(key)
            run_starts[data["run_id"]] = key
        elif event == "question.asked":
            if "count" in data:
                questions[key] = questions.get(key, 0) + data["count"]
            else:
                unknown_questions.add(key)
        elif event == "attempt.ended":
            attempts[key] = attempts.get(key, 0) + 1
            attempt_count += 1
            usage = data.get("usage", {})
            values = (usage.get("input"), usage.get("output"))
            if all(type(v) is int for v in values):
                executor += sum(values)
            else:
                complete = False
        elif event == "lane.escalated":
            escalated.add(key)
        elif event == "gate.result":
            gate_total += 1
            if data.get("verdict") == "accepted":
                gate_pass += 1
        elif event in ("gate.passed", "gate.failed"):
            gate_total += 1
            if event == "gate.passed":
                gate_pass += 1
        elif event == "task.state":
            last_state[key] = data.get("to")
        elif event == "l1.started":
            l1_records += 1
            l1_keys.add(key)
        elif event == "questions.form":
            digest = data.get("form_sha256")
            if isinstance(digest, str):
                plan_forms[digest] = data.get("count")
        elif event == "orchestrator.usage":
            samples += 1
            values = (data.get("input"), data.get("output"))
            if data.get("source") == "measured" and all(type(v) is int for v in values):
                orchestrator += sum(values)
            else:
                complete = False
    count = len(classified)
    lane_population = classified | dispatched
    round_tasks = classified | dispatched | set(attempts)
    latencies = [
        (datetime.fromisoformat(first_dispatched[key][:-1])
         - datetime.fromisoformat(first_classified[key][:-1])) // timedelta(milliseconds=1)
        for key in first_classified.keys() & first_dispatched.keys()
    ]
    accepted = sum(1 for key in dispatched if last_state.get(key) == "accepted")
    return {
        "orchestration_overhead": _metric(orchestrator / executor if complete and samples and executor and attempt_count else None, attempt_count),
        "time_to_first_dispatch_ms": _metric(statistics.median(latencies) if latencies else None, len(latencies)),
        "questions_per_task": _metric(sum(questions.get(key, 0) for key in classified) / count if count and not unknown_questions.intersection(classified) else None, count),
        "questions_per_plan": _metric(
            statistics.mean(plan_forms.values()) if plan_forms and all(
                type(v) is int for v in plan_forms.values()) else None,
            len(plan_forms)),
        "rounds_per_task": _metric(statistics.median(attempts.get(key, 0) for key in round_tasks) if round_tasks else None, len(round_tasks)),
        "lane_escalation_rate": _metric(
            len(escalated & lane_population) / len(lane_population) if lane_population else None,
            len(lane_population)),
        "gate_pass_rate": _metric(gate_pass / gate_total if gate_total else None, gate_total),
        "task_acceptance_rate": _metric(accepted / len(dispatched) if dispatched else None, len(dispatched)),
        "l1_calls": _metric(l1_records if dispatched else None, len(l1_keys)),
    }
