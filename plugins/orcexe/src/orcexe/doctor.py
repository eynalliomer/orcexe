"""Read-only, model-free diagnostics and privacy-bounded probe projections."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import sys
import time

from orcexe import fsio, gitutil, hosts, proc, redact, trust
from orcexe.adapters import CodexAdapter, ClaudeCodeAdapter, CursorAdapter
from orcexe.adapters.codex_appserver import AppServerError

# Seconds at production scale. Tests pass budget_scale without changing CLI defaults.
_BUDGET_S = dict(detect=10, auth=10, models=10, appserver=10, quota=10,
                 total=60, prerequisite=10, trust=10, kill_grace=.05)
_PROBES = {"detect": "detect", "auth_status": "auth", "list_models": "models", "quota": "quota"}

TOOLS = ("codex", "claude-code", "cursor")
STATUSES = ("pass", "warn", "fail", "info")
FORBIDDEN = frozenset(("email", "userEmail", "userInfo", "orgId", "orgName", "accountId",
                       "userId", "firstName", "lastName"))
_PLANS = ("free", "plus", "pro", "max", "team", "teams", "business", "enterprise",
          "education", "ultra", "pro_plus", "premium", "unknown")
_METHODS = ("unknown", "chatgpt", "api_key", "claude.ai", "oauth", "subscription")
_SOURCES = ("none", "app-server", "stream", "last-run", "cmd", "probe", "assumed")
_ENV_WARN = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL",
             "CURSOR_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY")
_EFFECTS = {
    "E1": "codex exec writes trust entries to config.toml; orcexe records them, see `orcexe trust list`",
    "C1": "--model changes your default model; orcexe restores it after each run",
}
_VERSION = re.compile(r"[0-9]+(?:\.[0-9]+)+(?:-[a-zA-Z0-9.-]+)?\Z")
_MODEL = re.compile(r"[a-z0-9][a-z0-9._:-]{0,127}\Z")


def _require(condition):
    if not condition:
        raise ValueError("Invalid orcexe.env.v1 report")


def _keys(value, required, optional=()):
    _require(isinstance(value, dict) and set(required) <= set(value) <= set(required) | set(optional))


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and not any(ord(c) < 32 for c in value)


def _number(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _private_tree(value, ancestors=()):
    _require(len(ancestors) < 32)
    if isinstance(value, (dict, list)):
        _require(id(value) not in ancestors)
        ancestors += (id(value),)
        for key, item in value.items() if isinstance(value, dict) else enumerate(value):
            if isinstance(value, dict):
                _require(isinstance(key, str) and key not in FORBIDDEN)
            _private_tree(item, ancestors)
    elif isinstance(value, str):
        # Reports are projected first; redaction is a final defence for strings.
        _require(redact.redact_text(value) == value)
        _require(not any(key in value for key in FORBIDDEN))
    else:
        _require(value is None or type(value) in (bool, int, float))
        if type(value) is float:
            _require(math.isfinite(value))


def _check(value, capability=False):
    _keys(value, ("code", "status", "evidence", "message", "fix") if capability else
          ("id", "category", "status", "message", "fix"))
    _require(value["status"] in STATUSES and _text(value["message"]))
    _require(value["fix"] is None or _text(value["fix"]))
    if value["status"] in ("warn", "fail"):
        _require(_text(value["fix"]))
    if capability:
        _require(value["code"] in tuple("H" + str(n) for n in range(1, 8)))
        _require(value["evidence"] in ("measured", "declared"))
    else:
        _require(_text(value["id"]) and value["category"] in ("prereq", "tool", "auth", "env", "host"))


def _validate_quota(value):
    _keys(value, ("windows", "allowed", "source"), ("age_s", "reached", "error"))
    _require(isinstance(value["windows"], list))
    _require(value["allowed"] is None or type(value["allowed"]) is bool)
    _require(value["source"] in _SOURCES)
    if "age_s" in value:
        _require(_number(value["age_s"]))
    if "reached" in value:
        _require(value["reached"] is None or value["reached"] in ("none", "primary", "secondary", "weekly", "daily", "monthly"))
    if "error" in value:
        _require(value["error"] == "probe failed")
    for window in value["windows"]:
        _keys(window, ("name", "used_pct", "resets_at"), ("window_mins",))
        _require(window["name"] in ("primary", "secondary", "five_hour", "seven_day", "weekly", "daily", "monthly"))
        _require(all(_number(v) for k, v in window.items() if k != "name"))


def validate_env(d):
    """Reject unknown keys, invalid types/vocabulary, PII keys and unsafe strings."""
    _private_tree(d)
    _keys(d, ("schema", "checked_at", "orcexe_version", "os", "python", "host", "tools", "checks", "summary"))
    _require(d["schema"] == "orcexe.env.v1")
    _require(isinstance(d["checked_at"], str) and re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", d["checked_at"]))
    datetime.strptime(d["checked_at"], "%Y-%m-%dT%H:%M:%SZ")
    _require(all(isinstance(d[k], str) and _VERSION.fullmatch(d[k]) for k in ("orcexe_version", "python")))
    _require(isinstance(d["os"], str) and re.fullmatch(r"(?:macos|linux|windows|unknown)-(?:arm64|x86_64|x86|unknown)", d["os"]))
    host = d["host"]
    _keys(host, ("tool", "detected_by", "capabilities"))
    _require(host["tool"] in (*TOOLS, "none"))
    _require(host["detected_by"] == ("env:CLAUDECODE" if host["tool"] == "claude-code" else "none"))
    _require(isinstance(host["capabilities"], list))
    for capability in host["capabilities"]:
        _check(capability, True)
    _require([v["code"] for v in host["capabilities"]] ==
             ["H" + str(n) for n in range(1, 3 if host["tool"] == "none" else 8)])
    _require(isinstance(d["tools"], list) and 1 <= len(d["tools"]) <= 3)
    seen = set()
    for tool in d["tools"]:
        _keys(tool, ("tool", "present", "version", "min_ok", "path", "auth", "method", "plan", "models", "quota", "warnings", "side_effects"))
        _require(tool["tool"] in TOOLS and tool["tool"] not in seen)
        seen.add(tool["tool"])
        _require(type(tool["present"]) is bool and type(tool["min_ok"]) is bool)
        _require(tool["version"] is None or isinstance(tool["version"], str) and _VERSION.fullmatch(tool["version"]))
        _require(tool["path"] is None or tool["path"] == _display_path(tool["tool"]))
        _require(tool["auth"] in ("ok", "missing", "unknown") and tool["method"] in _METHODS)
        _require(tool["plan"] is None or tool["plan"] in _PLANS)
        _require(tool["models"] is None or isinstance(tool["models"], list) and
                 all(isinstance(v, str) and _MODEL.fullmatch(v) for v in tool["models"]))
        _validate_quota(tool["quota"])
        _require(isinstance(tool["warnings"], list) and all(v in _EFFECTS.values() for v in tool["warnings"]))
        _require(isinstance(tool["side_effects"], list) and all(v in _EFFECTS for v in tool["side_effects"]))
    _require(isinstance(d["checks"], list))
    for check in d["checks"]:
        _check(check)
    _keys(d["summary"], STATUSES)
    for status in STATUSES:
        _require(type(d["summary"][status]) is int and d["summary"][status] ==
                 sum(c["status"] == status for c in d["checks"]))


def _budgets(scale):
    if type(scale) not in (int, float) or not math.isfinite(scale) or scale <= 0:
        raise ValueError("budget_scale must be finite and positive")
    return {name: value * scale for name, value in _BUDGET_S.items()}


class _BoundedProbes:
    def _binary(self):
        # Every adapter resolves its binary before each subprocess/session.
        # Recompute remaining time here so multi-command auth shares one budget,
        # while leaving parsing and process-tree cleanup with the adapters.
        deadline = getattr(self, "_doctor_deadline", None)
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("probe deadline exceeded")
            self._detect_timeout_s = remaining
            self._auth_timeout_s = remaining
            self._appserver_timeout_s = min(remaining, self._doctor_budgets["appserver"])
        return super()._binary()


class _DoctorCodex(_BoundedProbes, CodexAdapter):
    pass


class _DoctorClaude(_BoundedProbes, ClaudeCodeAdapter):
    pass


class _DoctorCursor(_BoundedProbes, CursorAdapter):
    pass


class _QuickCodex(_DoctorCodex):
    def _session(self):
        # auth_status still owns login parsing; skip its optional plan RPC too.
        raise AppServerError(None, "quick probe skips app-server")


def _adapter(tool, quick=False, *, budget_scale=1):
    budgets = _budgets(budget_scale)
    cls = {"codex": _QuickCodex if quick else _DoctorCodex,
           "claude-code": _DoctorClaude, "cursor": _DoctorCursor}[tool]
    # Do not even fetch billing overrides or host-marker values. Adapters get
    # a detached environment with those names absent, before their own filter.
    env = {name: os.environ[name] for name in os.environ
           if name not in (*_ENV_WARN, "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT")}
    options = dict(env=env, detect_timeout_s=budgets["detect"],
                   auth_timeout_s=budgets["auth"], kill_grace_s=budgets["kill_grace"])
    if tool == "codex":
        options["appserver_timeout_s"] = budgets["appserver"]
    adapter = cls(**options)
    adapter._doctor_budgets = budgets
    return adapter


def _display_path(tool):
    # Absolute binary paths can contain personal names; retain only tool identity.
    return "<private>/" + {"codex": "codex", "claude-code": "claude", "cursor": "agent"}[tool]


def _call(adapter, method, deadline=None):
    started = time.monotonic()
    configured_budget = adapter._doctor_budgets[_PROBES[method]]
    budget = (min(configured_budget, max(0, deadline - started))
              if deadline is not None else configured_budget)
    adapter._doctor_deadline = started + budget
    timed_out = False
    try:
        if budget <= 0:
            raise TimeoutError("doctor deadline exceeded")
        result = getattr(adapter, method)()
        result = result if isinstance(result, dict) else {}
    except TimeoutError:
        result, timed_out = {}, True
    except Exception:
        result = {}
    finally:
        adapter._doctor_deadline = None
    # Several adapters intentionally discard runner statuses (including the
    # optional Codex plan RPC). The shared deadline survives those projections.
    if timed_out or time.monotonic() - started >= budget:
        # Report the configured per-probe budget even if the shared total
        # deadline left less execution time (or no time at all).
        result = dict(result, _timeout_s=configured_budget)
        if method == "detect":
            result["present"] = adapter._binary() is not None
    return result


def _timeout_line(tool, operation, raw):
    if "_timeout_s" not in raw:
        return None
    binary = {"codex": "codex", "claude-code": "claude", "cursor": "agent", "git": "git"}[tool]
    return _line(tool + "." + operation, "auth" if operation == "auth" else "tool", "warn",
                 "{}: {} timed out after {:g} s".format(tool, operation, raw["_timeout_s"]),
                 "re-run `orcexe doctor --no-cache`; if it persists run `" + binary + " --version` yourself")


def _detect(raw, tool):
    version = raw.get("version")
    version = version if isinstance(version, str) and _VERSION.fullmatch(version) else None
    present = raw.get("present") is True
    return dict(present=present, version=version, min_ok=present and version is not None and raw.get("min_ok") is True,
                path=_display_path(tool) if present else None)


def _auth(raw):
    plan = raw.get("plan")
    plan = plan.lower() if isinstance(plan, str) else None
    return dict(state=raw.get("state") if raw.get("state") in ("ok", "missing", "unknown") else "unknown",
                method=raw.get("method") if raw.get("method") in _METHODS else "unknown",
                plan=plan if plan in _PLANS else None,
                source=raw.get("source") if raw.get("source") in ("probe", "assumed") else "probe")


def _models(raw):
    models = raw.get("models")
    if not isinstance(models, list):
        return None
    result = []
    for item in models:
        name = item.get("id") if isinstance(item, dict) else item
        if (not isinstance(name, str) or not _MODEL.fullmatch(name) or
                redact.redact_text(name) != name or any(key in name for key in FORBIDDEN)):
            return None
        if name not in result:
            result.append(name)
    return result


def _quota(raw):
    clean = dict(windows=[], allowed=raw.get("allowed") if type(raw.get("allowed")) is bool else None,
                 source=raw.get("source") if raw.get("source") in _SOURCES else "none")
    if _number(raw.get("age_s")):
        clean["age_s"] = raw["age_s"]
    if "reached" in raw:
        clean["reached"] = raw["reached"] if raw["reached"] in ("none", "primary", "secondary", "weekly", "daily", "monthly") else None
    for window in raw.get("windows", []) if isinstance(raw.get("windows"), list) else []:
        if not isinstance(window, dict):
            continue
        candidate = {key: window[key] for key in ("name", "used_pct", "resets_at", "window_mins") if key in window}
        try:
            _validate_quota(dict(windows=[candidate], allowed=None, source="none"))
            clean["windows"].append(candidate)
        except (ValueError, TypeError):
            continue
    if "error" in raw:
        clean["error"] = "probe failed"
    return clean


def _line(identifier, category, status, message, fix=None):
    return dict(id=identifier, category=category, status=status, message=message, fix=fix)


def _tool_report(adapter, quick, previous, deadline=None):
    tool = adapter.manifest.tool
    checks = [_line(tool + ".manifest", "tool", "pass", tool + " manifest valid")]
    raw = _call(adapter, "detect", deadline)
    detected = _detect(raw, tool)
    timeout = _timeout_line(tool, "detect", raw)
    auth = _auth({})
    models = None
    quota = _quota({})
    if not detected["present"]:
        checks.append(timeout or _line(tool + ".detect", "tool", "info", tool + " not installed"))
    else:
        checks.append(timeout or _line(tool + ".detect", "tool", "pass" if detected["min_ok"] else "warn",
                            tool + (" " + detected["version"] if detected["version"] else " version unknown"),
                            None if detected["min_ok"] else "install or update " + adapter.manifest.data["binary"][0] + " to " + adapter.manifest.data["min_version"] + " or newer"))
        raw = _call(adapter, "auth_status", deadline)
        auth = _auth(raw)
        checks.append(_timeout_line(tool, "auth", raw) or _line(tool + ".auth", "auth", "pass" if auth["state"] == "ok" else "warn",
                            tool + " authentication " + auth["state"] +
                            (" (" + auth["method"] + ", " + (auth["plan"] or "plan unknown") + ")" if auth["state"] == "ok" else ""),
                            None if auth["state"] == "ok" else "run `" + " ".join(adapter.manifest.data["auth"]["login_cmd"]) + "`"))
        raw = {}
        if quick and tool == "codex":
            models = previous.get("models") if previous else None
        else:
            raw = _call(adapter, "list_models", deadline)
            models = _models(raw)
        message = ("no model-free list" if tool == "claude-code" else
                   "models skipped (quick; cached ids only)" if quick and tool == "codex" else
                   "models unavailable" if models is None else str(len(models)) + " model ids")
        checks.append(_timeout_line(tool, "models", raw) or _line(tool + ".models", "tool", "info", tool + ": " + message))
        raw = {}
        if not quick:
            raw = _call(adapter, "quota", deadline)
            quota = _quota(raw)
        checks.append(_timeout_line(tool, "quota", raw) or _line(tool + (".quota.skipped" if quick else ".quota"), "tool", "info",
                            tool + (": quota skipped (quick)" if quick else ": quota " + quota["source"]
                                    + (" (cache age {:.0f}s)".format(quota["age_s"]) if "age_s" in quota else ""))))
        if quota["allowed"] is False:
            checks.append(_line(tool + ".quota.exhausted", "tool", "warn", tool + " quota exhausted",
                                "wait for quota reset or use another logged-in tool"))
    effects = [effect["id"] for effect in adapter.manifest.data["side_effects"] if effect["id"] in _EFFECTS]
    warnings = [_EFFECTS[key] for key in effects]
    for effect, message in zip(effects, warnings):
        checks.append(_line(tool + "." + effect, "tool", "warn", message,
                            "review `orcexe trust list`" if effect == "E1" else "let orcexe restore the default model after each run"))
    return dict(tool=tool, **detected, auth=auth["state"], method=auth["method"], plan=auth["plan"],
                models=models, quota=quota, warnings=warnings, side_effects=effects), checks


def git_version(text):
    """Accept portable Git suffixes, including 2.54.0.windows.1."""
    match = re.search(r"\bgit version (\d+)\.(\d+)(?:\.(\d+))?", text)
    return tuple(int(v or 0) for v in match.groups()) if match else None


def _prerequisites(repo, *, budget_scale=1, deadline=None):
    budgets = _budgets(budget_scale)
    checks = []
    system = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}.get(platform.system(), "unknown")
    machine = {"aarch64": "arm64", "arm64": "arm64", "amd64": "x86_64", "x86_64": "x86_64", "i386": "x86", "i686": "x86", "x86": "x86"}.get(platform.machine().lower(), "unknown")
    os_name = system + "-" + machine
    supported = system != "unknown" and machine != "unknown"
    checks.append(_line("os", "prereq", "pass" if supported else "fail", "OS/arch " + os_name,
                        None if supported else "use macOS, Linux or Windows on a supported architecture"))
    python_ok = sys.version_info >= (3, 9)
    python_version = ".".join(str(v) for v in sys.version_info[:3])
    checks.append(_line("python", "prereq", "pass" if python_ok else "fail", "Python " + python_version,
                        None if python_ok else "install Python 3.9 or newer"))
    output = []
    budget = budgets["prerequisite"]
    if deadline is not None:
        budget = min(budget, max(0, deadline - time.monotonic()))
    result = proc.run(["git", "--version"], cwd=str(repo), env={"PATH": os.environ.get("PATH", os.defpath)},
                      timeout_s=budget, stall_s=budget, on_line=output.append, max_line_bytes=4096,
                      kill_grace_s=budgets["kill_grace"])
    ran = result.status == "exited" and result.returncode == 0
    version = git_version("\n".join(output)) if ran else None
    git_ok = version is not None and version >= (2, 31, 0)
    checks.append(_line("git.worktree", "prereq", "pass" if git_ok else "fail",
                        "git " + (".".join(map(str, version)) if version else "unavailable or unrecognised"),
                        None if git_ok else "install Git 2.31 or newer"))
    if result.status in ("timeout", "stalled"):
        checks[-1] = _timeout_line("git", "version", {"_timeout_s": budget})
    try:
        free = shutil.disk_usage(repo).free
        enough = free >= 2 * 1024 ** 3
        message = "repo disk: {:.1f} GiB free (measured)".format(free / 1024 ** 3)
    except OSError:
        enough, message = False, "repo disk space unavailable"
    checks.append(_line("disk", "prereq", "pass" if enough else "warn", message,
                        None if enough else "free at least 2 GiB on the repository filesystem"))
    checks.append(_line("gh", "prereq", "info", "gh " + ("present" if shutil.which("gh") else "not installed (optional)")))
    return checks, os_name, python_version, ran


def _main_worktree(repo, budget, kill_grace):
    # A single bounded observation replaces the two independent gitutil calls;
    # retain its isolated Git environment and never expose repository paths.
    if budget <= 0:
        raise TimeoutError("doctor deadline exceeded")
    env = {key: os.environ[key] for key in
           ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT") if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", LC_ALL="C", LANG="C")
    lines = []
    result = proc.run(["git", "--no-pager", "worktree", "list", "--porcelain", "-z"],
                      cwd=str(repo), env=env, timeout_s=budget, stall_s=budget,
                      on_line=lines.append, kill_grace_s=kill_grace)
    if result.status in ("timeout", "stalled"):
        raise TimeoutError("git deadline exceeded")
    if result.status != "exited" or result.truncated_lines or result.pipes_held:
        raise gitutil.GitError("Incomplete worktree observation")
    if result.returncode == 128 and "not a git repository" in result.stderr_tail:
        return None
    output = "\n".join(lines)
    first = output.split("\0", 1)[0]
    if result.returncode or not first.startswith("worktree ") or not output.endswith("\0"):
        raise gitutil.GitError("Invalid worktree observation")
    return Path(first[len("worktree "):]).resolve()


def _trust_check(repo, *, budget_scale=1, deadline=None):
    budgets = _budgets(budget_scale)
    budget = budgets["trust"]
    if deadline is not None:
        budget = min(budget, max(0, deadline - time.monotonic()))
    try:
        main = _main_worktree(repo, budget, budgets["kill_grace"])
        if main is None:
            return _line("trust", "env", "info", "trust ledger: outside a Git repository")
        ledger = Path(main) / ".orcexe" / "trust-ledger.json"
        if not ledger.exists():
            return _line("trust", "env", "info", "trust ledger: 0 present entries")
        entries = trust.list_entries(ledger)
        present = [e for e in entries if e["state"] == "present"]
        gone = sum(not Path(e["key"]).is_dir() for e in present)
        return _line("trust", "env", "info", "trust ledger: {} present entries; {} missing directories".format(len(present), gone),
                     "orcexe trust clean" if gone else None)
    except TimeoutError:
        check = _timeout_line("git", "trust ledger", {"_timeout_s": budget})
        check.update(id="trust", category="env")
        return check
    except (OSError, ValueError, gitutil.GitError):
        return _line("trust", "env", "warn", "trust ledger unavailable", "inspect the repo trust ledger with `orcexe trust list`")


def _cache_path():
    return Path(os.environ.get("ORCEXE_STATE_DIR") or Path.home() / ".cache" / "orcexe") / "env.json"


def _read_cache(path):
    try:
        if path.stat().st_size > 1024 * 1024:
            return None
        data = fsio.read_json(path)
        validate_env(data)
        return data
    except (OSError, ValueError, TypeError, RecursionError):
        return None


def run_doctor(*, quick=False, tool=None, no_cache=False, repo=None, budget_scale=1):
    """Return (validated report, cached). No adapter run/login/install is called."""
    budgets = _budgets(budget_scale)
    deadline = time.monotonic() + budgets["total"]
    repo = Path(repo or Path.cwd()).resolve()
    selected = (tool,) if tool else TOOLS
    path = _cache_path()
    previous = _read_cache(path)
    if previous and not no_cache:
        age = time.time() - datetime.strptime(previous["checked_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
        compatible = [v["tool"] for v in previous["tools"]] == list(selected)
        was_quick = any(c["id"].endswith(".quota.skipped") for c in previous["checks"])
        file_age = time.time() - path.stat().st_mtime
        if 0 <= age < 60 and 0 <= file_age < 60 and compatible and (quick or not was_quick):
            return previous, True
    # Load all manifests before starting any work; unreadable manifests are fatal.
    adapters = [_adapter(name, quick, budget_scale=budget_scale) for name in selected]
    checks, os_name, python_version, subprocess_ok = _prerequisites(repo, budget_scale=budget_scale, deadline=deadline)
    old_tools = {v["tool"]: v for v in previous["tools"]} if previous else {}
    with ThreadPoolExecutor(max_workers=len(adapters)) as pool:
        futures = [pool.submit(_tool_report, adapter, quick, old_tools.get(adapter.manifest.tool), deadline) for adapter in adapters]
        tools = []
        for future in futures:
            report, lines = future.result()
            tools.append(report)
            checks.extend(lines)
    if not any(t["present"] and t["min_ok"] and t["auth"] == "ok" and t["quota"]["allowed"] is not False for t in tools):
        checks.append(_line("tools.usable", "tool", "fail", "no usable AI tool",
                            "install and log in to at least one of codex / claude / agent"))
    host = hosts.detect_host(subprocess_ok=subprocess_ok)
    for name in _ENV_WARN:
        if name in os.environ:
            if name == "ANTHROPIC_BASE_URL" and host["tool"] == "claude-code":
                checks.append(_line("env." + name.lower(), "env", "info",
                                    name + " set by the Claude Code host; orcexe never passes it to child tools"))
                continue
            target = "Claude" if name.startswith("ANTHROPIC_") else "Cursor" if name.startswith("CURSOR_") else "Codex"
            checks.append(_line("env." + name.lower(), "env", "warn",
                                name + " set: orcexe never passes it to " + target +
                                "; but your own " + target.lower() + " sessions will bill the API instead of your subscription",
                                "unset " + name + " for subscription sessions"))
    checks.append(_trust_check(repo, budget_scale=budget_scale, deadline=deadline))
    checks.extend(_line("host." + c["code"], "host", c["status"], c["message"], c["fix"]) for c in host["capabilities"])
    version = fsio.read_json(hosts.PLUGIN / ".claude-plugin" / "plugin.json")["version"]
    data = dict(schema="orcexe.env.v1", checked_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                orcexe_version=version, os=os_name, python=python_version, host=host, tools=tools, checks=checks,
                summary={status: sum(c["status"] == status for c in checks) for status in STATUSES})
    validate_env(data)
    fsio.atomic_write_json(path, data, mode=0o600)
    return data, False


def human_report(data, cached=False):
    lines = ["cached: true"] if cached else []
    for check in data["checks"]:
        lines.append("{:<4}  {}".format(check["status"].upper(), check["message"]))
        if check["fix"]:
            lines.append("      → " + check["fix"])
    lines.append("Summary: " + ", ".join(str(data["summary"][s]) + " " + s for s in STATUSES))
    return "\n".join(lines)


def run_probe(tool, operation, *, budget_scale=1):
    """Call exactly one public adapter probe; never preflight with detect()."""
    adapter = _adapter(tool, budget_scale=budget_scale)
    raw = _call(adapter, {"detect": "detect", "auth": "auth_status", "models": "list_models", "quota": "quota"}[operation])
    result = {"tool": tool, "probe": operation}
    if operation == "detect":
        result.update(_detect(raw, tool))
        code = 0 if result["present"] and result["version"] else 2
    elif operation == "auth":
        result.update(_auth(raw))
        code = {"ok": 0, "missing": 1, "unknown": 2}[result["state"]]
    elif operation == "models":
        result.update(models=_models(raw), source=raw.get("source") if raw.get("source") in _SOURCES else "none")
        code = 0 if result["models"] is not None else 2
    else:
        result.update(_quota(raw))
        recognised = (isinstance(raw.get("windows"), list) and "allowed" in raw and
                      (raw["allowed"] is None or type(raw["allowed"]) is bool) and
                      raw.get("source") in _SOURCES and
                      len(result["windows"]) == len(raw["windows"]))
        code = 0 if recognised else 2
    # Binary resolution is not a probe and cannot launch a process.
    if adapter._binary() is None:
        code = 2
    elif "error" in raw:
        result["error"] = "no model-free list" if tool == "claude-code" and operation == "models" else "probe failed"
        code = 2 if any(word in str(raw["error"]) for word in ("unavailable", "malformed", "invalid", "deadline", "exited")) else 1
    timeout = _timeout_line(tool, operation, raw)
    if timeout:
        result.update(status=timeout["status"], message=timeout["message"], fix=timeout["fix"])
        code = 1
    _private_tree(result)
    return result, code


def doctor_command(args):
    try:
        data, cached = run_doctor(quick=args.quick, tool=args.tool, no_cache=args.no_cache)
        print(json.dumps(data, ensure_ascii=False, allow_nan=False) if args.json else human_report(data, cached))
        return 1 if data["summary"]["fail"] else 0
    except Exception:
        message = "Doctor could not run; check manifests and state directory permissions."
        print(json.dumps({"error": message}) if args.json else message)
        return 2


def probe_command(args):
    try:
        data, code = run_probe(args.tool, args.operation)
        print(json.dumps(data, ensure_ascii=False, allow_nan=False) if args.json else
              args.tool + " " + args.operation + ": " + json.dumps({k: v for k, v in data.items() if k not in ("tool", "probe")}))
        return code
    except Exception:
        message = "Probe could not run; check the tool manifest and installation."
        print(json.dumps({"error": message}) if args.json else message)
        return 2
