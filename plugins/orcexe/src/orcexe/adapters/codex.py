"""Codex executor and model-free probes.

v0.1 supports ChatGPT login only: OPENAI_API_KEY and CODEX_API_KEY are never
forwarded, even through allow_extra. Account payloads are transient; only the
account.planType string leaves auth_status. Codex does not report model_actual.
"""

import hashlib
import json
import math
import os
from pathlib import Path
import re
import threading

from orcexe import fsio, gitutil, proc, redact
from orcexe.adapters.base import Adapter, RunRequest
from orcexe.adapters.codex_appserver import AppServer, AppServerError
from orcexe.manifest import load_manifest


_MANIFEST = Path(__file__).resolve().parents[3] / "adapters" / "codex.yaml"
_USAGE = {"input": "input_tokens", "cached_read": "cached_input_tokens",
          "cache_write": "cache_write_input_tokens", "output": "output_tokens",
          "reasoning": "reasoning_output_tokens"}


class CodexAdapter(Adapter):
    def __init__(self, manifest=None, *, appserver_timeout_s=10, auth_timeout_s=None,
                 **options):
        super().__init__(load_manifest(_MANIFEST) if manifest is None else manifest, **options)
        self._appserver_timeout_s = appserver_timeout_s
        self._auth_timeout_s = self._detect_timeout_s if auth_timeout_s is None else auth_timeout_s
        self._probe_lock = threading.Lock()
        # Base's extraction hook has no run/path argument. Thread-local context
        # binds final.md and warnings without sharing them across concurrent runs.
        self._run_context = threading.local()

    def _environment(self):
        env = super()._environment()
        return {name: value for name, value in env.items()
                if name.upper() not in ("OPENAI_API_KEY", "CODEX_API_KEY")}

    def _session(self):
        binary = self._binary()
        if binary is None:
            raise AppServerError(None, "binary unavailable")
        return AppServer(binary, self._environment(), self._appserver_timeout_s,
                         kill_grace_s=self._kill_grace_s)

    @staticmethod
    def _reason(error):
        # Do not expose server messages, which may contain account identifiers.
        if isinstance(error, AppServerError) and error.code is not None:
            return "app-server error"
        if isinstance(error, AppServerError) and error.message in (
                "binary unavailable", "deadline exceeded", "spawn failed", "stdin closed",
                "pipe read failed", "line too long", "server exited", "malformed JSON",
                "malformed response", "malformed error", "missing result"):
            return "app-server: " + error.message
        if isinstance(error, ValueError) and str(error) == "pagination limit":
            return "app-server pagination limit"
        return "app-server unavailable or invalid response"

    def auth_status(self):
        result = {"state": "unknown", "method": "unknown", "plan": None, "source": "probe"}
        with self._probe_lock:
            binary = self._binary()
            if binary is not None:
                method = ["unknown"]

                def on_line(line):
                    if "ChatGPT" in line:
                        method[0] = "chatgpt"
                    elif "API key" in line and method[0] != "chatgpt":
                        method[0] = "api_key"

                observed = proc.run([binary, *self.manifest.data["auth"]["status_cmd"][1:]],
                    cwd=os.getcwd(), env=self._environment(), timeout_s=self._auth_timeout_s,
                    stall_s=self._auth_timeout_s, on_line=on_line, on_stderr_line=on_line,
                    max_line_bytes=4096,
                    kill_grace_s=self._kill_grace_s)
                if observed.status == "exited":
                    result["state"] = {0: "ok", 1: "missing"}.get(observed.returncode, "unknown")
                result["method"] = method[0]
            try:
                with self._session() as session:
                    payload = session.call("account/read", {"refreshToken": False})
                    plan = payload["account"]["planType"]
                    if isinstance(plan, str):
                        result["plan"] = plan
            except (AppServerError, OSError, ValueError, KeyError, TypeError):
                pass
        return result

    def list_models(self):
        try:
            with self._probe_lock, self._session() as session:
                models, cursor = [], None
                for _ in range(5):
                    params = {} if cursor is None else {"cursor": cursor}
                    payload = session.call("model/list", params)
                    for model in payload["data"] if "data" in payload else payload["models"]:
                        name = model["id"]
                        if not isinstance(name, str):
                            raise ValueError("invalid model id")
                        item = {"id": name}
                        if "supportedReasoningEfforts" in model:
                            efforts = model["supportedReasoningEfforts"]
                            item["supported_efforts"] = []
                            for effort in efforts if isinstance(efforts, list) else []:
                                if isinstance(effort, dict):
                                    effort = next((effort[key] for key in
                                        ("reasoningEffort", "effort", "name")
                                        if isinstance(effort.get(key), str)), None)
                                if isinstance(effort, str):
                                    item["supported_efforts"].append(effort)
                        if "defaultReasoningEffort" in model:
                            default = model["defaultReasoningEffort"]
                            item["default_effort"] = default if isinstance(default, str) else None
                        models.append(item)
                    cursor = payload["nextCursor"]
                    if cursor is None:
                        return {"models": models, "source": "app-server"}
                    if not isinstance(cursor, str) or not cursor:
                        raise ValueError("invalid cursor")
                raise ValueError("pagination limit")
        except (AppServerError, OSError, ValueError, KeyError, TypeError) as error:
            return {"models": None, "source": "none", "error": self._reason(error)}

    def quota(self):
        try:
            with self._probe_lock, self._session() as session:
                payload = session.call("account/rateLimits/read")
                limits = payload["rateLimits"]
                allowed = payload["ordinaryUsageAllowed"]
                reached = limits["rateLimitReachedType"]
                if type(allowed) is not bool or reached is not None and not isinstance(reached, str):
                    raise ValueError("invalid limits")
                windows = []
                for name in ("primary", "secondary"):
                    window = limits[name]
                    if window is None and name == "secondary":
                        continue
                    values = [window[key] for key in ("usedPercent", "windowDurationMins", "resetsAt")]
                    if not all(type(v) in (int, float) and math.isfinite(v) and v >= 0 for v in values):
                        raise ValueError("invalid window")
                    windows.append(dict(zip(("name", "used_pct", "window_mins", "resets_at"),
                                            [name, *values])))
                return {"windows": windows, "allowed": allowed, "reached": reached,
                        "source": "app-server", "age_s": 0}
        except (AppServerError, OSError, ValueError, KeyError, TypeError) as error:
            return {"windows": [], "allowed": None, "source": "none", "error": self._reason(error)}

    def _extract(self, events):
        text, usage = "", {}
        for event in events:
            if not isinstance(event, dict):
                continue
            item = event.get("item")
            if (event.get("type") == "item.completed" and isinstance(item, dict) and
                    item.get("type") == "agent_message" and isinstance(item.get("text"), str)):
                text = item["text"]
            if event.get("type") == "turn.completed":
                raw = event.get("usage")
                raw = raw if isinstance(raw, dict) else {}
                usage = {name: raw.get(key) if type(raw.get(key)) is int and raw[key] >= 0 else None
                         for name, key in _USAGE.items()}
        context = getattr(self._run_context, "value", None)
        final = ""
        if context is not None:
            try:
                final = context["final"].read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                pass
        warnings = []
        if final:
            text = final
        elif text:
            warnings.append("final_text_from_stream")
            if context is not None:
                context["warnings"].extend(warnings)
        return {"final_text": text, "usage": {name: usage.get(name) for name in _USAGE},
                "model_actual": None, "warnings": warnings}

    @staticmethod
    def _message(event):
        value = event.get("error", event.get("message", ""))

        def decode(value, depth=0):
            if depth >= 8:
                return value
            if isinstance(value, dict):
                return {key: decode(item, depth + 1) for key, item in value.items()}
            if isinstance(value, str):
                try:
                    parsed = json.loads(value)
                except (ValueError, RecursionError):
                    return value
                if isinstance(parsed, (dict, str)):
                    return decode(parsed, depth + 1)
            return value

        # Errors can wrap another JSON string inside error.message. Decode
        # those carriers while preserving structured HTTP status/code fields.
        value = decode(value)
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False)
        return value if isinstance(value, str) else ""

    def _classify(self, summary, proc_result):
        counts = summary["counts"]
        if counts.get("turn.completed") and not counts.get("turn.failed") and proc_result.returncode == 0:
            return "completed", []
        messages = [self._message(event) for event in summary["events"]
                    if isinstance(event, dict) and event.get("type") in ("error", "turn.failed")]
        messages = [message for message in messages if message]
        if messages:
            summary["last_error"] = redact.redact_text(messages[-1])[:500]
        evidence = "\n".join(messages).lower()
        if "unauthorized" in evidence or re.search(r'http\s*401\b|"(?:status|status_code)"\s*:\s*401\b', evidence):
            return "auth", []
        if "model_not_found" in evidence:
            return "failed", ["model_not_found"]
        if re.search(r"usage limit|rate limit|quota", evidence):
            quota = self.quota()
            if (quota.get("allowed") is False or quota.get("reached") is not None or
                    any(window.get("used_pct", 0) >= 100 for window in quota["windows"])):
                return "quota", []
            return "failed", ["quota_suspected"]
        if proc_result.returncode == 0 and not counts.get("turn.completed"):
            return "failed", ["final_event_missing"]
        return "failed", ["unknown_failure"]

    def _trust_headers(self):
        home = self._env.get("CODEX_HOME")
        if not home:
            user_home = self._env.get("HOME") or self._env.get("USERPROFILE")
            if not user_home:
                return set()
            home = str(Path(user_home) / ".codex")
        headers = set()
        try:
            with (Path(home) / "config.toml").open(encoding="utf-8") as stream:
                for line in stream:
                    if line.startswith("[projects."):
                        headers.add(line.rstrip("\r\n"))
                    # Keep only table headers, never configuration values.
                    del line
        except (OSError, UnicodeError):
            pass
        return headers

    def run(self, req):
        if not isinstance(req, RunRequest):
            return super().run(req)
        watch_trust = req.profile == "write" and gitutil.is_repo(req.workdir)
        before = self._trust_headers() if watch_trust else set()
        context = {"final": Path(req.run_dir).resolve() / "final.md", "warnings": []}
        self._run_context.value = context
        try:
            result = super().run(req)
            if watch_trust:
                context["warnings"].extend("trust_entry_added:" + hashlib.sha256(
                    header.encode("utf-8")).hexdigest()[:12]
                    for header in sorted(self._trust_headers() - before))
            if context["warnings"]:
                result["warnings"] = list(dict.fromkeys(result["warnings"] + context["warnings"]))
                fsio.atomic_write_json(Path(req.run_dir) / "result.json", result, mode=0o600)
            return result
        finally:
            del self._run_context.value
