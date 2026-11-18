"""Claude Code subscription executor and model-free authentication probe.

Never forward ANTHROPIC_* or CLAUDE_CODE_USE_*: these can switch authentication,
billing or providers, even when explicitly requested through allow_extra.
-p runs user/project hooks. --bare is unsuitable for OAuth/keychain subscribers.
--add-dir grants access only to run_dir/brief, containing brief.md; acceptEdits
also permits edits there. Evidence remains outside that grant. result.json is written
by this adapter afterwards. total_cost_usd is list price, never a billed cost.
"""

from collections import Counter, deque
import json
import math
import os
from pathlib import Path
import re
import threading
import time

from orcexe.adapters.base import (safe_write_text, safe_write_json, safe_append_jsonl, reserve_attempt, non_json_text)

from orcexe import gitutil, proc, redact
from orcexe.adapters.base import (Adapter, RunRequest, validate_run_result,
                                  _ARTIFACTS, _USAGE as _RESULT_USAGE, _require)
from orcexe.manifest import load_manifest


_MANIFEST = Path(__file__).resolve().parents[3] / "adapters" / "claude.yaml"
_USAGE = {"input": "input_tokens", "cached_read": "cache_read_input_tokens",
          "cache_write": "cache_creation_input_tokens", "output": "output_tokens"}
_AUTH_STDOUT_LIMIT = 64 * 1024


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _quota_info(info):
    """Normalize only measured quota fields; never persist arbitrary payloads."""
    if not isinstance(info, dict) or info.get("status") not in ("allowed", "rejected"):
        return None
    clean = {"status": info["status"]}
    for key in ("rateLimitType", "overageStatus", "overageDisabledReason"):
        if isinstance(info.get(key), str):
            clean[key] = info[key]
    if _number(info.get("resetsAt")):
        clean["resetsAt"] = info["resetsAt"]
    if type(info.get("isUsingOverage")) is bool:
        clean["isUsingOverage"] = info["isUsingOverage"]
    windows = info.get("unifiedWindows")
    clean["unifiedWindows"] = {}
    for name in ("five_hour", "seven_day"):
        window = windows.get(name) if isinstance(windows, dict) else None
        if (isinstance(window, dict) and _number(window.get("utilization")) and
                _number(window.get("resetsAt"))):
            value = {key: window[key] for key in ("utilization", "resetsAt")}
            if window.get("status") in ("allowed", "rejected"):
                value["status"] = window["status"]
            clean["unifiedWindows"][name] = value
    return clean


def _windows(info):
    return [dict(name=name, used_pct=window["utilization"] * 100,
                 resets_at=window["resetsAt"])
            for name, window in info["unifiedWindows"].items()]


class ClaudeCodeAdapter(Adapter):
    def __init__(self, manifest=None, *, auth_timeout_s=None, **options):
        super().__init__(load_manifest(_MANIFEST) if manifest is None else manifest, **options)
        self._auth_timeout_s = self._detect_timeout_s if auth_timeout_s is None else auth_timeout_s
        self._run_context = threading.local()

    def _environment(self):
        return {name: value for name, value in super()._environment().items()
                if not name.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))}

    def auth_status(self):
        result = dict(state="unknown", method="unknown", plan=None, source="probe")
        binary = self._binary()
        if binary is None:
            return result
        # Auth stdout is one document (pretty-printed in 2.1.310), not JSONL.
        # Keep a bounded buffer only until parsing; never log account details.
        stdout = bytearray()
        oversized = False

        def on_line(line):
            nonlocal oversized
            if oversized:
                return
            data = line.encode("utf-8")
            if len(stdout) + len(data) + bool(stdout) > _AUTH_STDOUT_LIMIT:
                oversized = True
                stdout.clear()
                return
            if stdout:
                stdout.extend(b"\n")
            stdout.extend(data)

        raw = None
        try:
            observed = proc.run([binary, *self.manifest.data["auth"]["status_cmd"][1:]],
                cwd=os.getcwd(), env=self._environment(), on_line=on_line,
                max_line_bytes=_AUTH_STDOUT_LIMIT, stderr_tail_bytes=0,
                timeout_s=self._auth_timeout_s, stall_s=self._auth_timeout_s,
                kill_grace_s=self._kill_grace_s)
            if (observed.status != "exited" or oversized or observed.pipes_held or
                    observed.truncated_lines or observed.stdout_bytes > _AUTH_STDOUT_LIMIT):
                return result
            try:
                raw = json.loads(stdout.decode("utf-8"))
                fields = {key: raw[key]
                          for key in ("loggedIn", "authMethod", "subscriptionType", "apiProvider")
                          if isinstance(raw, dict) and
                          ((key == "loggedIn" and type(raw.get(key)) is bool) or
                           (key != "loggedIn" and isinstance(raw.get(key), str)))}
            except (ValueError, RecursionError):
                return result
            finally:
                raw = None
                stdout.clear()
            result.update(state={True: "ok", False: "missing"}.get(fields.get("loggedIn"), "unknown"),
                          method=fields.get("authMethod", "unknown"), plan=fields.get("subscriptionType"))
            return result
        finally:
            raw = None
            stdout.clear()

    def list_models(self):
        return dict(models=None, source="none", error="no model-free model list in claude CLI")

    def _cache_path(self):
        state = self._env.get("ORCEXE_STATE_DIR")
        if not state:
            home = self._env.get("HOME") or self._env.get("USERPROFILE")
            state = str(Path(home) / ".cache" / "orcexe") if home else str(Path.home() / ".cache" / "orcexe")
        return Path(state) / "quota-claude.json"

    def quota(self):
        try:
            raw = json.loads(self._cache_path().read_text(encoding="utf-8"))
            info = _quota_info(raw.get("rate_limit_info"))
            observed = raw.get("observed_at")
            if info is not None and _number(observed):
                now = time.time()
                age = max(0, now - observed)
                if age > 6 * 60 * 60:
                    return dict(windows=[], allowed=None, source="none", age_s=age)
                active = [w for w in _windows(info) if w["resets_at"] > now]
                limiting = info["unifiedWindows"].get(info.get("rateLimitType"), {})
                reset = (info.get("resetsAt", math.inf) <= now or
                         limiting.get("resetsAt", math.inf) <= now or
                         bool(info["unifiedWindows"]) and not active)
                # A reset is unknown capacity until a fresh stream observation.
                allowed = None if reset else info["status"] == "allowed"
                return dict(windows=active, allowed=allowed, source="stream", age_s=age)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            pass
        return dict(windows=[], allowed=None, source="none")

    def _ensure_brief_readable(self, req):
        # run() has validated every --add-dir, including the explicit brief
        # grant. External briefs therefore need no base default-root restriction.
        pass

    def _extract(self, events):
        context = getattr(self._run_context, "value", None)
        # Keep bounded facts captured directly from the child, never read back
        # executor-writable artifacts as status or model evidence.
        if context is not None and "observed" in context:
            events = context["observed"].values()
        extracted = dict(final_text="", usage={}, model_actual=None)
        latest, result = None, None
        rejected = context.get("rejected", False) if context is not None else False
        evidence = set(context.get("markers", ())) if context is not None else set()
        for event in events:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if kind == "system" and event.get("subtype") == "init" and isinstance(event.get("model"), str):
                extracted["model_actual"] = event["model"]
            elif kind == "rate_limit_event":
                info = _quota_info(event.get("rate_limit_info"))
                if info is not None:
                    latest = info
                    rejected |= info["status"] == "rejected"
            elif kind == "result":
                result = event

        if result is not None:
            text = result.get("result")
            extracted["final_text"] = text if isinstance(text, str) else ""
            usage = result.get("usage")
            usage = usage if isinstance(usage, dict) else {}
            extracted["usage"] = {key: usage.get(value) if type(usage.get(value)) is int and usage[value] >= 0 else None
                                  for key, value in _USAGE.items()}
            extracted["usage"]["reasoning"] = None
            cost = result.get("total_cost_usd")
            if _number(cost):
                extracted["cost"] = dict(list_usd=cost, basis="tool")
        if latest is not None:
            extracted["quota_after"] = dict(windows=_windows(latest), source="stream")
        if context is not None:
            context.update(extracted=extracted, latest=latest, result=result,
                           rejected=rejected, markers=set(evidence))
        return extracted

    @staticmethod
    def _error_markers(text):
        text = text.lower()
        markers = []
        if (any(value in text for value in ("please run /login", "invalid api key", "authentication_failed")) or
                re.search(r"http\s*401\b", text)):
            markers.append("auth")
        if "there's an issue with the selected model" in text or "model_not_found" in text:
            markers.append("model_not_found")
        return markers

    def _classify(self, summary, proc_result):
        context = getattr(self._run_context, "value", None)
        events = [event for event in summary["events"] if isinstance(event, dict)]
        results = [event for event in events if event.get("type") == "result"]
        result = context.get("result", results[-1] if results else None) if context is not None else (results[-1] if results else None)
        rejected = context.get("rejected", False) if context is not None else any(
            event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict) and
            event["rate_limit_info"].get("status") == "rejected" for event in events)
        markers = set()
        # Only an explicitly erroneous final result can carry event diagnostics.
        if result is not None and result.get("is_error") is True:
            markers.update(self._error_markers(json.dumps(result)))
            if any(result.get(key) in {"auth", "auth_error", "authentication_error", "authentication_failed", "error_authentication"}
                   for key in ("subtype", "terminal_reason")):
                markers.add("auth")
        if proc_result.returncode != 0:
            markers.update(context.get("markers", ()) if context is not None else ())
            markers.update(self._error_markers(non_json_text(proc_result.stderr_tail)))
            for event in events if context is None else ():
                if isinstance(event.get("_raw"), str):
                    markers.update(self._error_markers(non_json_text(event["_raw"])))
        if rejected:
            return "quota", []
        if "auth" in markers:
            return "auth", []
        if "model_not_found" in markers:
            return "failed", ["model_not_found"]
        if (result is not None and result.get("subtype") == "success" and
                result.get("is_error") is False and result.get("terminal_reason") == "completed" and
                proc_result.returncode == 0):
            return "completed", []
        if proc_result.returncode == 0 and result is None:
            return "failed", ["final_event_missing"]
        return "failed", []

    def _blocked(self, req):
        """No process has run: emit a normal, append-only preflight receipt."""
        directory = Path(req.run_dir).resolve()
        if (directory / "result.json").exists():
            raise FileExistsError("result.json already exists")
        reserve_attempt(directory)
        paths = {key: str(directory / name) for key, name in (
            ("final_text_path", "final.md"), ("events_path", "events.jsonl"), ("stderr_tail_path", "stderr.txt"))}
        for path in paths.values():
            safe_write_text(path, "", mode=0o600)
        safe_write_text(directory / "brief" / "brief.md", Path(req.brief_path).read_text(encoding="utf-8"), mode=0o600)
        result = dict(schema="orcexe.run.v1", run_id=req.run_id, task=req.task, tool=req.tool,
            tool_version=None, family=self.manifest.family, model_requested=req.model,
            model_actual=None, effort=req.effort, profile=req.profile, context="fresh", status="blocked",
            exit_code=None, signal=None, head_before=None, head_after=None, dirty_before=[],
            diff_files=[], untracked=[], self_report=dict(exit=None, needs=[]),
            usage={key: None for key in (*_USAGE, "reasoning")}, cost=dict(list_usd=None, basis="unknown"),
            quota_after=dict(windows=[], source="none"), duration_ms=0,
            warnings=["add_dir_invalid"], **paths)
        validate_run_result(result, run_dir=directory)
        safe_write_json(directory / "result.json", result, mode=0o600)
        return result

    def run(self, req):
        if not isinstance(req, RunRequest):
            return super().run(req)
        req.__post_init__()
        if req.tool != self.manifest.tool:
            raise ValueError("request tool differs from manifest")
        directory = Path(req.run_dir).resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        (directory / "brief").mkdir(mode=0o700)
        values = dict(workdir=str(Path(req.workdir).resolve()), run_dir=str(directory),
                      model=req.model, effort=req.effort, final=str(directory / "final.md"), prompt="Preflight")
        argv = self.manifest.build_argv(req.profile, **{
            name: values[name] for name in self.manifest.placeholders(req.profile)})
        grants = []
        for index, token in enumerate(argv):
            if token == "--add-dir":
                grants.append(argv[index + 1] if index + 1 < len(argv) else "")
            elif token.startswith("--add-dir="):
                grants.append(token.partition("=")[2])
        if not grants or any(not os.path.isabs(path) or not Path(path).is_dir() for path in grants):
            return self._blocked(req)
        # The brief grant must be present even in a caller-supplied manifest.
        if directory / "brief" not in [Path(path).resolve() for path in grants]:
            return self._blocked(req)
        context = {"directory": directory}
        self._run_context.value = context
        try:
            result = self._run(req)
            extracted = context.get("extracted", {})
            for key in ("cost", "quota_after"):
                if key in extracted:
                    result[key] = extracted[key]
            info = context.get("latest")
            if info is not None:
                try:
                    safe_write_json(self._cache_path(), dict(observed_at=context.get("quota_observed_at", time.time()), rate_limit_info=info), mode=0o600)
                except OSError:
                    result["warnings"].append("quota_cache_write_failed")
            validate_run_result(result, run_dir=directory)
            safe_write_json(directory / "result.json", result, mode=0o600)
            return result
        finally:
            del self._run_context.value

    def _observe(self, event, *, diagnostic=False):
        context = self._run_context.value
        facts = context.setdefault("observed", {})
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            facts["init"] = {key: event.get(key) for key in ("type", "subtype", "model")}
        elif kind == "result":
            facts[kind] = {key: event.get(key) for key in ("type", "subtype", "is_error",
                "terminal_reason", "result", "usage", "total_cost_usd")}
        elif kind == "rate_limit_event":
            info = _quota_info(event.get("rate_limit_info"))
            if info is not None:
                facts[kind] = dict(type=kind, rate_limit_info=info)
                context["quota_observed_at"] = time.time()
                if info["status"] == "rejected":
                    context["rejected"] = True
        if diagnostic and isinstance(event.get("_raw"), str):
            context.setdefault("markers", set()).update(self._error_markers(non_json_text(event["_raw"])))

    def _run(self, req):
        """Shared lifecycle invariants, with bounded Claude facts captured directly."""
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
        status, warnings = "blocked", []
        observed = None
        extracted = {"final_text": "", "usage": {}, "model_actual": None}
        detected = {"version": None}
        # Always create the evidence artifacts, even for a refused write.
        paths = {key: str(directory / name) for key, name in _ARTIFACTS.items()}
        safe_write_text(directory / "brief" / "brief.md", Path(req.brief_path).read_text(encoding="utf-8"))
        safe_write_text(paths["events_path"], "", mode=0o600)
        if (req.profile == "write" and root ==
                Path(gitutil.main_worktree(req.workdir))):
            warnings.append("main_worktree_write_refused")
        else:
            self._ensure_brief_readable(req)
            prompt = "Read the task brief at {} and carry it out. Follow its rules exactly.".format(
                directory / "brief" / "brief.md")
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

            # --output-format stream-json emits one complete JSON object per
            # line, unlike the auth status document; JSONL parsing is intentional.
            def on_line(line):
                diagnostic = False
                try:
                    event = json.loads(line, parse_constant=lambda _: None)
                except (ValueError, RecursionError):
                    event = {"_raw": line}
                    diagnostic = True
                event = redact.redact_obj(event)
                self._observe(event, diagnostic=diagnostic)
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
        after = gitutil.changed_since(req.workdir, before_head, before_dirty)
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
            usage={key: extracted.get("usage", {}).get(key) for key in _RESULT_USAGE},
            cost={"list_usd": None, "basis": "unknown"},
            quota_after={"windows": [], "source": "none"},
            duration_ms=int(observed.duration_s * 1000) if observed else 0,
            warnings=list(dict.fromkeys(warnings)), **paths)
        validate_run_result(result, run_dir=directory)
        safe_write_json(directory / "result.json", result, mode=0o600)
        return result
