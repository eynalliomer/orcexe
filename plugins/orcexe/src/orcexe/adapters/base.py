"""Observed runner outcomes are authoritative; agent claims are informational.

Brief delivery: stdin is DEVNULL (T06), and Windows limits the entire argv to
roughly 32K. Copy the brief into run_dir/brief.md and send only a <=600 character
pointer. For writes, the default readable external location is the main repo's
.orcexe/runs/. Callers normally allocate runs there. Other external locations
require a subclass's _ensure_brief_readable implementation and tool-specific
read grants; base refuses them rather than silently sending an unreadable brief.

Artifact paths are canonical absolute paths, siblings under run_dir with fixed
names. The schema has no run_dir field: standalone validation derives it from
final_text_path; callers with a trusted run_dir can additionally bind validation
to that directory. Git path lists are repository-root-relative, never artifacts.
"""

from abc import ABC, abstractmethod
from collections import Counter, deque
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import threading
import secrets
import stat

from orcexe import envfilter, gitutil, proc, redact
from orcexe.manifest import Manifest, meets_min_version


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
_HEAD = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_STATUSES = {"completed", "failed", "quota", "auth", "timeout", "stalled",
             "blocked", "cancelled"}
_EXITS = {"DONE", "DONE_WITH_CONCERNS", "BLOCKED", "NEEDS_CONTEXT"}
_USAGE = ("input", "cached_read", "cache_write", "output", "reasoning")
_ARTIFACTS = {"final_text_path": "final.md", "events_path": "events.jsonl",
              "stderr_tail_path": "stderr.txt"}
_STRINGS = ("schema", "run_id", "task", "tool", "family", "model_requested",
            "effort", "profile", "context", "status")
_NULLABLE_STRINGS = ("tool_version", "model_actual", "head_before", "head_after")
_LISTS = ("dirty_before", "diff_files", "untracked", "warnings")
_KEYS = set(_STRINGS + _NULLABLE_STRINGS + _LISTS) | set(_ARTIFACTS) | {
    "exit_code", "signal", "self_report", "usage", "cost", "quota_after", "duration_ms"}


def _write_parent(path):
    """Open each parent without following executor-planted directory links."""
    path = Path(path).absolute()
    # macOS system aliases are outside the executor's writable namespace.
    if len(path.parts) > 1 and path.parts[1] in ("tmp", "var", "etc"):
        prefix = Path(path.anchor) / path.parts[1]
        path = prefix.resolve().joinpath(*path.parts[2:])
    fd = os.open(path.anchor, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in path.parts[1:-1]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd, path.name
    except BaseException:
        os.close(fd)
        raise


def safe_write_bytes(path, data, mode=0o600):
    """Atomic publication; neither existing destinations nor parents may be links."""
    parent, name = _write_parent(path)
    temporary = "." + name + "-" + secrets.token_hex(8)
    try:
        try:
            check = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
        except FileNotFoundError:
            pass
        else:
            try:
                if not stat.S_ISREG(os.fstat(check).st_mode):
                    raise ValueError("unsafe output file")
            finally:
                os.close(check)
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     mode, dir_fd=parent)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), mode)
        os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)
    finally:
        try:
            os.unlink(temporary, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.close(parent)


def safe_write_text(path, text, mode=0o600):
    safe_write_bytes(path, text.encode("utf-8"), mode)


def safe_write_json(path, obj, mode=0o600):
    safe_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2,
                                   sort_keys=True, allow_nan=False) + "\n", mode)


def safe_append_jsonl(path, obj):
    parent, name = _write_parent(path)
    try:
        fd = os.open(name, os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK,
                     dir_fd=parent)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise ValueError("unsafe event file")
            line = json.dumps(obj, ensure_ascii=True, sort_keys=True, allow_nan=False)
            handle.write(line + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(parent)


def reserve_attempt(directory):
    parent, name = _write_parent(Path(directory) / ".attempt")
    try:
        fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600, dir_fd=parent)
        os.close(fd)
    finally:
        os.close(parent)


def non_json_text(text):
    """Diagnostic lines only: JSON event contents are never error evidence."""
    lines = []
    for line in text.splitlines():
        try:
            json.loads(line)
        except (ValueError, RecursionError):
            lines.append(line)
    return "\n".join(lines)


def _require(condition, message):
    if not condition:
        raise ValueError(message)


def _text(value):
    return isinstance(value, str) and bool(value) and "\0" not in value


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    task: str
    tool: str
    model: str
    effort: str
    profile: str
    workdir: str
    brief_path: str
    run_dir: str
    timeout_s: float = 1800
    stall_s: float = 300

    def __post_init__(self):
        _require(isinstance(self.run_id, str) and _RUN_ID.fullmatch(self.run_id),
                 "invalid run_id")
        for key in ("task", "tool", "model", "effort"):
            _require(_text(getattr(self, key)), "invalid " + key)
        _require(self.profile in ("write", "review"), "invalid profile")
        for key in ("workdir", "brief_path", "run_dir"):
            value = getattr(self, key)
            _require(isinstance(value, (str, os.PathLike)) and
                     os.path.isabs(value) and "\0" not in os.fspath(value),
                     key + " must be absolute")
        _require(Path(self.workdir).is_dir(), "workdir must be a directory")
        _require(Path(self.brief_path).is_file(), "brief_path must be an existing file")
        for key in ("timeout_s", "stall_s"):
            value = getattr(self, key)
            _require(type(value) in (int, float) and math.isfinite(value) and value > 0,
                     key + " must be finite and positive")


def validate_run_result(d, *, run_dir=None):
    """Raise ValueError on schema/type/path/invariant violations; return None."""
    _require(isinstance(d, dict) and set(d) == _KEYS, "invalid result keys")
    for key in _STRINGS:
        _require(_text(d[key]), "invalid " + key)
    for key in _NULLABLE_STRINGS:
        _require(d[key] is None or _text(d[key]), "invalid " + key)
    for key in ("head_before", "head_after"):
        _require(d[key] is None or _HEAD.fullmatch(d[key]), "invalid " + key)
    _require(d["schema"] == "orcexe.run.v1", "invalid schema")
    _require(_RUN_ID.fullmatch(d["run_id"]), "invalid run_id")
    _require(d["context"] == "fresh", "invalid context")
    _require(d["profile"] in ("review", "write"), "invalid profile")
    _require(d["status"] in _STATUSES, "invalid status")
    for key in ("exit_code", "signal"):
        _require(d[key] is None or type(d[key]) is int and d[key] >= 0,
                 "invalid " + key)
    _require(d["signal"] is None or d["signal"] > 0, "invalid signal")
    _require(d["signal"] is None or d["exit_code"] is None,
             "signal and exit_code are mutually exclusive")
    _require(type(d["duration_ms"]) is int and d["duration_ms"] >= 0,
             "invalid duration_ms")
    parent = None if run_dir is None else Path(run_dir).resolve()
    for key, name in _ARTIFACTS.items():
        value = d[key]
        _require(_text(value) and os.path.isabs(value), "invalid " + key)
        path = Path(value)
        _require(str(path.resolve()) == value and path.name == name, "unsafe " + key)
        if parent is None:
            parent = path.parent
        _require(path.parent == parent, "artifact outside run_dir")
    for key in _LISTS:
        _require(isinstance(d[key], list) and all(_text(v) for v in d[key]),
                 "invalid " + key)
        if key != "warnings":
            for value in d[key]:
                path = PurePosixPath(value)
                _require(not path.is_absolute() and ".." not in path.parts and
                         value != "." and not re.match(r"^[A-Za-z]:", value) and
                         not value.startswith("\\"), "unsafe repository path")
    report = d["self_report"]
    _require(isinstance(report, dict) and set(report) == {"exit", "needs"},
             "invalid self_report")
    _require(report["exit"] is None or isinstance(report["exit"], str) and
             report["exit"] in _EXITS, "invalid self_report.exit")
    _require(isinstance(report["needs"], list) and all(_text(v) for v in report["needs"]),
             "invalid self_report.needs")
    usage = d["usage"]
    _require(isinstance(usage, dict) and set(usage) == set(_USAGE), "invalid usage")
    _require(all(v is None or type(v) is int and v >= 0 for v in usage.values()),
             "invalid usage values")
    cost = d["cost"]
    _require(isinstance(cost, dict) and set(cost) == {"list_usd", "basis"}, "invalid cost")
    value = cost["list_usd"]
    _require(value is None or type(value) in (int, float) and math.isfinite(value) and value >= 0,
             "invalid cost.list_usd")
    _require(isinstance(cost["basis"], str) and cost["basis"] in ("tool", "unknown"),
             "invalid cost.basis")
    quota = d["quota_after"]
    _require(isinstance(quota, dict) and set(quota) == {"windows", "source"},
             "invalid quota_after")
    _require(isinstance(quota["windows"], list) and
             all(isinstance(v, dict) for v in quota["windows"]) and
             isinstance(quota["source"], str) and quota["source"] in ("stream", "none"),
             "invalid quota_after values")
    _require(d["status"] != "completed" or d["exit_code"] == 0, "completed requires exit 0")
    _require(not (d["profile"] == "write" and not d["diff_files"] and
                  d["status"] == "completed") or "empty_diff" in d["warnings"],
             "missing empty_diff warning")
    _require(d["head_before"] == d["head_after"] or "head_changed" in d["warnings"],
             "missing head_changed warning")
    _require(d["model_actual"] is None or d["model_actual"] == d["model_requested"] or
             "model_mismatch" in d["warnings"], "missing model_mismatch warning")


class Adapter(ABC):
    """One execution lifecycle for all tools, with bounded, redacted evidence.

    env is a source mapping, never passed through directly. allow_extra names
    are explicitly tool-specific; envfilter's secret deny rule still applies.
    _classify receives {events: last 32 redacted events, counts: type counters}.
    _extract receives those last events. Counts cover the entire stream;
    unexpected event type names share a counter, bounding even hostile input.
    """

    def __init__(self, manifest, *, env=None, allow_extra=(), detect_timeout_s=5,
                 kill_grace_s=0.15):
        if not isinstance(manifest, Manifest):
            raise TypeError("Adapter requires a validated Manifest")
        self.manifest = manifest
        self._env = os.environ if env is None else dict(env)
        self._allow_extra = tuple(allow_extra)
        self._detect_timeout_s = detect_timeout_s
        self._kill_grace_s = kill_grace_s
        self._active = {}
        self._lock = threading.Lock()

    def _environment(self):
        return envfilter.child_env(self._env, allow_extra=self._allow_extra)[0]

    def _binary(self):
        override = self._env.get("ORCEXE_BIN_" + self.manifest.tool.upper().replace("-", "_"))
        candidates = (override,) if override else self.manifest.data["binary"]
        for candidate in candidates:
            path = shutil.which(candidate, path=self._environment().get("PATH", os.defpath))
            if path:
                return str(Path(path).absolute())
        return None

    def detect(self):
        path = self._binary()
        found = {"present": path is not None, "version": None, "path": path, "min_ok": False}
        if path is None:
            return found
        lines = deque(maxlen=8)
        command = [path, *self.manifest.data["version_cmd"][1:]]
        result = proc.run(command, cwd=os.getcwd(), env=self._environment(),
                          timeout_s=self._detect_timeout_s, stall_s=self._detect_timeout_s,
                          on_line=lines.append, max_line_bytes=4096,
                          kill_grace_s=self._kill_grace_s)
        if result.status == "exited" and result.returncode == 0:
            match = re.search(self.manifest.data["version_regex"], "\n".join(lines))
            if match:
                version = match.group(1) if match.lastindex else match.group(0)
                found["version"] = version
                found["min_ok"] = meets_min_version(version, self.manifest.data["min_version"])
        return found

    def login_hint(self):
        command = list(self.manifest.data["auth"]["login_cmd"])
        command[0] = self._binary() or command[0]
        return command

    def capabilities(self):
        return self.manifest.data

    def cancel(self, run_id):
        with self._lock:
            event = self._active.get(run_id)
            if event is None:
                return False
            event.set()
            return True

    @abstractmethod
    def auth_status(self):
        pass

    @abstractmethod
    def list_models(self):
        pass

    @abstractmethod
    def quota(self):
        pass

    @abstractmethod
    def _classify(self, events_summary, proc_result):
        pass

    @abstractmethod
    def _extract(self, events):
        pass

    def _ensure_brief_readable(self, req):
        if req.profile != "write":
            return
        directory = Path(req.run_dir).resolve()
        roots = (Path(req.workdir).resolve(),
                 Path(gitutil.main_worktree(req.workdir)) / ".orcexe" / "runs")
        _require(any(directory == root or root in directory.parents for root in roots),
                 "external write brief requires tool-specific read grants")

    def run(self, req):
        if not isinstance(req, RunRequest):
            raise ValueError("run requires RunRequest")
        req.__post_init__()  # Filesystem validity can change after construction.
        _require(req.tool == self.manifest.tool, "request tool differs from manifest")
        directory = Path(req.run_dir).resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # An exclusive reservation also prevents concurrent attempts racing on
        # the same directory, including across adapter instances/processes.
        if (directory / "result.json").exists():
            raise FileExistsError("result.json already exists")
        reserve_attempt(directory)
        root = Path(gitutil.toplevel(req.workdir))

        def repository_files(files):
            # Runner artifacts must never manufacture an apparent agent diff,
            # including when the repository does not ignore .orcexe/ yet.
            return [name for name in files if not (
                root / name == directory or directory in (root / name).parents)]

        before_head = gitutil.head(req.workdir)
        before_dirty = repository_files(gitutil.dirty(req.workdir))
        before_fingerprints = gitutil.fingerprints(req.workdir, before_dirty)
        status, warnings = "blocked", []
        observed = None
        extracted = {"final_text": "", "usage": {}, "model_actual": None}
        detected = {"version": None}
        # Always create the evidence artifacts, even for a refused write.
        paths = {key: str(directory / name) for key, name in _ARTIFACTS.items()}
        safe_write_text(directory / "brief.md", Path(req.brief_path).read_text(encoding="utf-8"))
        safe_write_text(paths["events_path"], "", mode=0o600)
        if (req.profile == "write" and root ==
                Path(gitutil.main_worktree(req.workdir))):
            warnings.append("main_worktree_write_refused")
        else:
            self._ensure_brief_readable(req)
            prompt = "Read the task brief at {} and carry it out. Follow its rules exactly.".format(
                directory / "brief.md")
            _require(len(prompt) <= 600 and not prompt.startswith("-"), "brief pointer too long")
            values = dict(workdir=str(Path(req.workdir).resolve()), run_dir=str(directory),
                model=req.model, effort=req.effort, final=paths["final_text_path"], prompt=prompt)
            argv = self.manifest.build_argv(req.profile, **{
                name: values[name] for name in self.manifest.placeholders(req.profile)})
            detected = self.detect()
            # A missing binary must produce a spawn_error, never fall back to a
            # different CLI when an explicit binary override was requested.
            argv[0] = detected["path"] or self._env.get(
                "ORCEXE_BIN_" + req.tool.upper().replace("-", "_"), argv[0])
            recent, counts = deque(maxlen=32), Counter()

            def on_line(line):
                try:
                    event = json.loads(line, parse_constant=lambda _: None)
                except (ValueError, RecursionError):
                    event = {"_raw": line}
                event = redact.redact_obj(event)
                safe_append_jsonl(paths["events_path"], event)
                recent.append(event)
                kind = event.get("type") if isinstance(event, dict) else None
                declared = self.manifest.data["events"]["types"]
                if not isinstance(kind, str) or not any(
                        kind == v for v in declared if not v.endswith(".*")):
                    kind = "_other"
                counts[kind] += 1

            cancel_event = threading.Event()
            with self._lock:
                _require(req.run_id not in self._active, "run_id already active")
                self._active[req.run_id] = cancel_event
            try:
                observed = proc.run(argv, cwd=str(req.workdir), env=self._environment(),
                    timeout_s=req.timeout_s, stall_s=req.stall_s, on_line=on_line,
                    cancel_event=cancel_event, kill_grace_s=self._kill_grace_s)
            finally:
                with self._lock:
                    del self._active[req.run_id]
            extracted = self._extract(list(recent))
            runner_status = {"timeout": "timeout", "stalled": "stalled",
                             "cancelled": "cancelled", "spawn_error": "failed"}
            if observed.status in runner_status:
                status = runner_status[observed.status]
            else:
                status, extra = self._classify({"events": list(recent), "counts": dict(counts)}, observed)
                warnings.extend(extra)
                if status == "completed" and observed.returncode != 0:
                    status = "failed"
            if observed.pipes_held:
                warnings.append("pipes_held")
        after = gitutil.changed_since(req.workdir, before_head, before_dirty, before_fingerprints)
        after["diff_files"] = repository_files(after["diff_files"])
        after["untracked"] = repository_files(after["untracked"])
        if before_head != after["head"]:
            warnings.append("head_changed")
        if req.profile == "write" and not after["diff_files"]:
            warnings.append("empty_diff")
        actual = extracted.get("model_actual")
        if actual is not None and actual != req.model:
            warnings.append("model_mismatch")
        final_text = redact.redact_text(extracted.get("final_text") or "")
        # Tool-written final files are not trusted as a second status channel.
        safe_write_text(paths["final_text_path"], final_text, mode=0o600)
        safe_write_text(paths["stderr_tail_path"],
            redact.redact_text(observed.stderr_tail if observed else ""), mode=0o600)
        final_line = final_text.rstrip("\r\n").split("\n")[-1]
        match = re.fullmatch(r"ORCEXE_EXIT: (DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT)", final_line)
        code = observed.returncode if observed else None
        result = dict(schema="orcexe.run.v1", run_id=req.run_id, task=req.task, tool=req.tool,
            tool_version=detected["version"], family=self.manifest.family,
            model_requested=req.model, model_actual=actual, effort=req.effort, profile=req.profile,
            context="fresh", status=status, exit_code=code if code is not None and code >= 0 else None,
            signal=-code if code is not None and code < 0 else None,
            head_before=before_head, head_after=after["head"], dirty_before=before_dirty,
            diff_files=after["diff_files"], untracked=after["untracked"],
            self_report={"exit": match[1] if match else None, "needs": []},
            usage={key: extracted.get("usage", {}).get(key) for key in _USAGE},
            cost={"list_usd": None, "basis": "unknown"},
            quota_after={"windows": [], "source": "none"},
            duration_ms=int(observed.duration_s * 1000) if observed else 0,
            warnings=list(dict.fromkeys(warnings)), **paths)
        validate_run_result(result, run_dir=directory)
        safe_write_json(directory / "result.json", result, mode=0o600)
        return result
