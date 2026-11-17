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
it validates only the last record, not the history. There is no repair.

The chain hashes exact line bytes without LF, not reserialized objects. Like
any unanchored hash chain it cannot detect suffix deletion, final-record data
edits, or an attacker recomputing the chain. Those require an external trusted
head, which this format does not provide. File fsync does not fsync directories.
"""

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import statistics
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
    "orchestrator.usage": dict(input="counter?", output="counter?", source="source"),
}


class LedgerError(RuntimeError):
    """A safe reason, optionally with a 1-based line number; never file data."""

    def __init__(self, line, reason=None):
        self.line = line if reason is not None else None
        self.reason = reason if reason is not None else line
        super().__init__("line {}: {}".format(line, reason) if reason is not None else line)


def default_path(main_worktree):
    return Path(main_worktree) / ".orcexe" / "ledger.jsonl"


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


class Ledger:
    def __init__(self, path, *, clock=None):
        """clock() returns an aware datetime; UTC milliseconds are recorded."""
        self.path = Path(path)
        self.clock = clock if clock is not None else lambda: datetime.now(timezone.utc)

    def append(self, event, task, data):
        if not isinstance(event, str) or event not in _SHAPES:
            raise ValueError("unknown ledger event")
        if not isinstance(task, str) or not _TASK.fullmatch(task) or redact_text(task) != task:
            raise ValueError("invalid task id")
        if not isinstance(data, dict):
            raise TypeError("ledger data must be a dict")
        clean = _sanitize(data, _SHAPES[event])
        fsio._make_parents(self.path.parent, 0o700)
        flags = os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_BINARY", 0)
        fd = os.open(self.path, flags, 0o600)
        try:
            with _lock(fd):
                last, previous = _tail(fd)
                if last and last["seq"] == _MAX_INT:
                    raise ValueError("invalid seq: integer bound exceeded")
                now = self.clock()
                if not isinstance(now, datetime) or now.utcoffset() is None:
                    raise ValueError("ledger clock must return an aware datetime")
                stamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
                if last and stamp < last["ts"]:
                    raise LedgerError("clock_regression")
                record = dict(schema=_SCHEMA, seq=last["seq"] + 1 if last else 1,
                              ts=stamp, event=event, task=task, data=clean, prev=previous)
                line = _serialize(record) + b"\n"
                if os.write(fd, line) != len(line):
                    raise LedgerError("short_write")
                os.fsync(fd)
                return record
        finally:
            os.close(fd)

    def records(self, *, task=None, event=None):
        """Yield matching records, checking even filtered-out lines in order."""
        try:
            fd = os.open(self.path, (os.O_RDWR if os.name == "nt" else os.O_RDONLY) | getattr(os, "O_BINARY", 0))
        except FileNotFoundError:
            return
        try:
            with _lock(fd, exclusive=False):
                # Keep the locked descriptor open until the lock is released.
                with os.fdopen(fd, "rb", closefd=False) as handle:
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
                        if (task is None or record["task"] == task) and (event is None or record["event"] == event):
                            yield record
        finally:
            os.close(fd)

    def verify(self):
        count = sum(1 for _ in self.records())
        return {"records": count, "last_seq": count, "ok": True}


def _metric(value=None, n=0):
    return {"value": value, "status": "measured" if value is not None else "not_seen", "n": n}


def kpis(ledger, task=None):
    """Aggregate a verified stream, optionally restricted to one task.

    n is the number of contributing tasks; for overhead it is the number of
    executor attempts. Overhead is known only with at least one orchestrator
    sample and attempt, all samples marked measured, both token counters known
    for every sample/attempt, and positive executor tokens. cached_read,
    cache_write and reasoning are not added (they may overlap input/output).
    Questions are total questions / classified tasks; rounds are the median
    attempts over classified tasks and tasks with attempts (including zero
    for classified tasks without attempts). Escalations / classified
    tasks and final accepted gates / tasks with gates are task proportions.
    Unobserved populations or missing counts/verdicts produce not_seen.
    Dispatch latency is the first dispatch timestamp minus the first
    classification timestamp per task, in milliseconds; the aggregate is the
    median over tasks with both events (n is the number of these tasks).
    """
    classified, questions, attempts, escalated, gates = set(), {}, {}, set(), {}
    first_classified, first_dispatched = {}, {}
    unknown_questions = set()
    executor = orchestrator = samples = attempt_count = 0
    complete = True
    for record in ledger.records(task=task):
        key, event, data = record["task"], record["event"], record["data"]
        if event == "task.classified":
            classified.add(key)
            first_classified.setdefault(key, record["ts"])
        elif event == "task.dispatched":
            first_dispatched.setdefault(key, record["ts"])
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
            gates[key] = data.get("verdict")
        elif event == "orchestrator.usage":
            samples += 1
            values = (data.get("input"), data.get("output"))
            if data.get("source") == "measured" and all(type(v) is int for v in values):
                orchestrator += sum(values)
            else:
                complete = False
    count = len(classified)
    round_tasks = classified.union(attempts)
    latencies = [
        (datetime.fromisoformat(first_dispatched[key][:-1])
         - datetime.fromisoformat(first_classified[key][:-1])) // timedelta(milliseconds=1)
        for key in first_classified.keys() & first_dispatched.keys()
    ]
    return {
        "orchestration_overhead": _metric(orchestrator / executor if complete and samples and executor and attempt_count else None, attempt_count),
        "time_to_first_dispatch_ms": _metric(statistics.median(latencies) if latencies else None, len(latencies)),
        "questions_per_task": _metric(sum(questions.get(key, 0) for key in classified) / count if count and not unknown_questions.intersection(classified) else None, count),
        "rounds_per_task": _metric(statistics.median(attempts.get(key, 0) for key in round_tasks) if round_tasks else None, len(round_tasks)),
        "lane_escalation_rate": _metric(len(escalated.intersection(classified)) / count if count else None, count),
        "acceptance_rate": _metric(sum(verdict == "accepted" for verdict in gates.values()) / len(gates) if gates and all(verdict is not None for verdict in gates.values()) else None, len(gates)),
    }
