"""Cursor subscription executor; model-free probes never retain account data.

Cursor print mode runs .cursor/hooks.json preToolUse/afterFileEdit AND the
project's .claude/settings.json PreToolUse hooks, sometimes twice per tool.
Measured stop hooks did not fire. Thinking events are discarded before storage.
With --sandbox enabled, shell tools run inside Cursor's sandbox. Explicit
shell refusals are failed/shell_blocked; shell usage is reported separately.
--model persists the default model: restore only its five config keys after
execution. Leave statsig-cache.json alone (analytics cache).

{model} in the manifest is the resolved live slug. Receipts record that concrete
slug as model_requested, so effort suffixes cannot manufacture model mismatches.
A cold execution needs one model-free models probe; a cached unknown model is
refused without any process, and an unknown model never spawns an executor.
"""
from collections import Counter, deque
import json
import os
from pathlib import Path
import re
import stat
import threading

from orcexe import envfilter, fsio, gitutil, proc, redact
from orcexe.adapters.base import (Adapter, RunRequest, validate_run_result,
                                  _ARTIFACTS, _USAGE, _require)
from orcexe.manifest import load_manifest

_MANIFEST = Path(__file__).resolve().parents[3] / 'adapters' / 'cursor.yaml'
_EFFORTS = ('low', 'medium', 'high', 'xhigh', 'max')
_LIMIT = 64 * 1024
_CONFIG_LIMIT = 256 * 1024
_MODEL_KEYS = ('model', 'selectedModel', 'modelSelectionHistory',
               'modelParameters', 'hasChangedDefaultModel')


def _display(text):
    return re.sub(r'(?: \((?:current|default)\))+$', '', text)


def _base(slug):
    base, separator, effort = slug.rpartition('-')
    return (base, effort) if separator and effort in _EFFORTS else (slug, None)


class CursorAdapter(Adapter):
    def __init__(self, manifest=None, *, auth_timeout_s=None, **options):
        super().__init__(load_manifest(_MANIFEST) if manifest is None else manifest, **options)
        self._auth_timeout_s = self._detect_timeout_s if auth_timeout_s is None else auth_timeout_s
        self._models = None
        self._context = threading.local()

    def _environment(self):
        return envfilter.child_env(self._env,
            allow_extra=(*self._allow_extra, 'CURSOR_CONFIG_DIR'))[0]

    def _config_path(self):
        directory = self._env.get('CURSOR_CONFIG_DIR') or (
            Path(self._env.get('HOME') or self._env.get('USERPROFILE') or Path.home()) / '.cursor')
        return Path(directory) / 'cli-config.json'

    @staticmethod
    def _read_config(path):
        # Bound bytes, not characters, and never include contents in diagnostics.
        with path.open('rb') as handle:
            mode = stat.S_IMODE(os.fstat(handle.fileno()).st_mode)
            payload = handle.read(_CONFIG_LIMIT + 1)
        if len(payload) > _CONFIG_LIMIT:
            raise ValueError('Cursor config exceeds size limit')
        def invalid_constant(_):
            raise ValueError('Invalid JSON constant')
        data = json.loads(payload.decode('utf-8'), parse_constant=invalid_constant)
        if not isinstance(data, dict):
            raise ValueError('Cursor config must be an object')
        return data, mode

    def _snapshot_model(self, path, warnings):
        try:
            data, mode = self._read_config(path)
        except FileNotFoundError:
            return None
        except (OSError, ValueError, RecursionError):
            warnings.append('cursor_config_unreadable')
            return None
        # Membership records absence independently of JSON null.
        return {key: data[key] for key in _MODEL_KEYS if key in data}, mode

    def _restore_model(self, path, snapshot, warnings):
        if snapshot is None:
            return
        values, mode = snapshot
        try:
            data, _ = self._read_config(path)
            current = {key: data[key] for key in _MODEL_KEYS if key in data}
            # JSON comparison distinguishes booleans from integers, unlike ==.
            if json.dumps(current, sort_keys=True) == json.dumps(values, sort_keys=True):
                return
            for key in _MODEL_KEYS:
                if key in values:
                    data[key] = values[key]
                else:
                    data.pop(key, None)
            # atomic_write_json sorts keys; preserve the re-read object's order.
            fsio.atomic_write_text(path, json.dumps(data, ensure_ascii=False,
                indent=2, allow_nan=False) + '\n', mode=mode)
        except (OSError, ValueError, RecursionError):
            warnings.append('cursor_config_unreadable')
            return
        warnings.append('cursor_default_model_restored')

    def _probe(self, args, limit=_LIMIT):
        binary = self._binary()
        if binary is None:
            return None
        stdout = bytearray()
        oversized = False

        def receive(line):
            nonlocal oversized
            data = line.encode('utf-8')
            if oversized or len(stdout) + len(data) + bool(stdout) > limit:
                oversized = True
                stdout.clear()
                return
            if stdout:
                stdout.extend(b'\n')
            stdout.extend(data)

        try:
            observed = proc.run([binary, *args], cwd=os.getcwd(), env=self._environment(),
                on_line=receive, max_line_bytes=limit, stderr_tail_bytes=0,
                timeout_s=self._auth_timeout_s, stall_s=self._auth_timeout_s,
                kill_grace_s=self._kill_grace_s)
            if (observed.status != 'exited' or observed.returncode != 0 or oversized or
                    observed.pipes_held or observed.truncated_lines or observed.stdout_bytes > limit):
                return None
            return stdout.decode('utf-8')
        finally:
            stdout.clear()

    def _probe_fields(self, args, names):
        text = self._probe(args)
        if text is None:
            return {}
        try:
            data = json.loads(text)
            return {key: data.get(key) for key in names} if isinstance(data, dict) else {}
        except (ValueError, RecursionError):
            return {}
        finally:
            text = None

    def auth_status(self):
        status = self._probe_fields(['status', '--format', 'json'], ('isAuthenticated',))
        value = status.get('isAuthenticated')
        state = ('ok' if value else 'missing') if type(value) is bool else 'unknown'
        about = self._probe_fields(['about', '--format', 'json'], ('subscriptionTier',))
        plan = about.get('subscriptionTier')
        return dict(state=state, method='unknown', plan=plan if isinstance(plan, str) else None,
                    source='probe')

    def list_models(self):
        text = self._probe(['models'])
        none = dict(models=None, source='none')
        if text is None:
            self._models = None
            return none
        slugs = {}
        for line in text.splitlines():
            if not line or line == 'Available models' or line.startswith('Tip:'):
                continue
            match = re.fullmatch(r'([a-z0-9][a-z0-9._-]*) - (\S.*)', line)
            if match is None or match[1] in slugs:
                self._models = None
                return none
            slugs[match[1]] = _display(match[2])
        if not slugs:
            self._models = None
            return none
        self._models = slugs
        groups = {}
        defaults = {}
        for slug, display in slugs.items():
            base, effort = _base(slug)
            entry = groups.setdefault(base, dict(id=base, display=display,
                supported_efforts=[], default_effort=None))
            if effort is None:
                entry['display'] = display
            else:
                entry['supported_efforts'].append(effort)
            # A current model is a user choice, not evidence of a default effort.
            if ' (default)' in text.split(slug + ' - ', 1)[1].splitlines()[0] and effort:
                defaults[base] = effort
        for base, entry in groups.items():
            entry['supported_efforts'] = [e for e in _EFFORTS if e in entry['supported_efforts']]
            entry['default_effort'] = defaults.get(base)
        return dict(models=list(groups.values()), source='cmd')

    def quota(self):
        return dict(windows=[], allowed=None, source='none')

    def _ensure_brief_readable(self, req):
        # Validated --add-dir grants the external brief directory explicitly.
        pass

    def _extract(self, events):
        context = getattr(self._context, 'value', {})
        facts = context.get('facts')
        if facts is not None:
            events = facts.values()
        result = dict(final_text='', usage={}, model_actual=None,
                      cost=dict(list_usd=None, basis='unknown'))
        for event in events:
            if not isinstance(event, dict):
                continue
            if event.get('type') == 'system' and event.get('subtype') == 'init':
                display = event.get('model')
                if isinstance(display, str):
                    matches = [slug for slug, name in (self._models or {}).items()
                               if name == _display(display)]
                    result['model_actual'] = matches[0] if len(matches) == 1 else None
            if event.get('type') == 'result':
                text = event.get('result')
                result['final_text'] = text if isinstance(text, str) else ''
                usage = event.get('usage')
                usage = usage if isinstance(usage, dict) else {}
                result['usage'] = {key: usage.get(name) if type(usage.get(name)) is int and usage[name] >= 0 else None
                    for key, name in dict(input='inputTokens', cached_read='cacheReadTokens',
                                          cache_write='cacheWriteTokens', output='outputTokens').items()}
                result['usage']['reasoning'] = None
        return result

    @staticmethod
    def _markers(text):
        text = text.lower()
        markers = set()
        for marker, phrases in (
                ('model_not_found', ('cannot use this model',)),
                ('auth', ('login required', 'please run agent login', 'authentication required')),
                ('untrusted', ('untrusted',)),
                ('quota_suspected', ('usage limit', 'usage-limit', 'rate limit', 'quota exceeded')),
                ('shell_blocked', ('shell command rejected', 'shell command blocked', 'shell permission denied'))):
            if any(phrase in text for phrase in phrases):
                markers.add(marker)
        return markers

    @classmethod
    def _event_markers(cls, event):
        # Paths, event types and model names are not diagnostic messages.
        if not isinstance(event, dict):
            return set()
        markers = set()
        for key, value in event.items():
            if isinstance(value, dict):
                markers.update(cls._event_markers(value))
            elif key in ('_raw', 'result', 'message', 'error', 'text', 'content') and isinstance(value, str):
                markers.update(cls._markers(value))
        return markers

    def _observe(self, event):
        context = self._context.value
        context.setdefault('markers', set()).update(self._event_markers(event))
        facts = context.setdefault('facts', {})
        if not isinstance(event, dict):
            return
        if event.get('type') == 'system' and event.get('subtype') == 'init':
            facts['init'] = {key: event.get(key) for key in ('type', 'subtype', 'model')}
        elif event.get('type') == 'result':
            facts['result'] = {key: event.get(key) for key in ('type', 'subtype', 'is_error', 'result', 'usage')}

    def _classify(self, summary, proc_result):
        context = getattr(self._context, 'value', {})
        events = summary['events']
        results = [e for e in events if isinstance(e, dict) and e.get('type') == 'result']
        result = context.get('facts', {}).get('result', results[-1] if results else None)
        markers = set(context.get('markers', ()))
        markers.update(self._markers(proc_result.stderr_tail))
        for event in events:
            markers.update(self._event_markers(event))
        if 'model_not_found' in markers:
            return 'failed', ['model_not_found']
        if 'auth' in markers:
            return 'auth', []
        if 'untrusted' in markers:
            return 'blocked', []
        success = (result is not None and result.get('subtype') == 'success' and
                   result.get('is_error') is False and proc_result.returncode == 0)
        if not success:
            for warning in ('quota_suspected', 'shell_blocked'):
                if warning in markers:
                    return 'failed', [warning]
        if result is None:
            return 'failed', ['final_event_missing']
        return ('completed', []) if success else ('failed', [])

    def run(self, req):
        if not isinstance(req, RunRequest):
            raise ValueError('run requires RunRequest')
        req.__post_init__()
        _require(req.tool == self.manifest.tool, 'request tool differs from manifest')
        directory = Path(req.run_dir).resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (directory / 'result.json').exists() or (directory / '.attempt').exists():
            raise FileExistsError('attempt already exists')
        context = dict(slug=req.model, warnings=[], preflight=None)
        self._context.value = context
        try:
            values = dict(workdir=str(Path(req.workdir).resolve()), run_dir=str(directory),
                          model=req.model, effort=req.effort, final=str(directory / 'final.md'), prompt='Preflight')
            argv = self.manifest.build_argv(req.profile, **{
                name: values[name] for name in self.manifest.placeholders(req.profile)})
            grants = [argv[i + 1] for i, token in enumerate(argv[:-1]) if token == '--add-dir']
            grants += [token.partition('=')[2] for token in argv if token.startswith('--add-dir=')]
            if (not grants or any(not os.path.isabs(p) or not Path(p).is_dir() for p in grants) or
                    directory not in [Path(p).resolve() for p in grants]):
                context.update(preflight='blocked', warnings=['add_dir_invalid'])
            elif req.profile == 'write' and Path(gitutil.toplevel(req.workdir)) == Path(gitutil.main_worktree(req.workdir)):
                # The common lifecycle refuses main writes before any probe.
                pass
            else:
                if self._models is None:
                    self.list_models()
                if self._models is None:
                    context.update(preflight='failed', warnings=['models_unavailable'])
                elif req.model + '-' + req.effort in self._models:
                    context['slug'] = req.model + '-' + req.effort
                elif req.model in self._models and not any(_base(s)[0] == req.model and _base(s)[1] for s in self._models):
                    context['warnings'].append('effort_not_applicable')
                else:
                    context.update(preflight='failed', warnings=['model_not_found'])
            return self._run(req)
        finally:
            del self._context.value

    def _run(self, req):
        if not isinstance(req, RunRequest):
            raise ValueError("run requires RunRequest")
        req.__post_init__()  # Filesystem validity can change after construction.
        _require(req.tool == self.manifest.tool, "request tool differs from manifest")
        directory = Path(req.run_dir).resolve()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        # An exclusive reservation also prevents concurrent attempts racing on
        # the same directory, including across adapter instances/processes.
        reservation = directory / ".attempt"
        if (directory / "result.json").exists():
            raise FileExistsError("result.json already exists")
        with reservation.open("x", encoding="utf-8"):
            pass
        root = Path(gitutil.toplevel(req.workdir))

        def repository_files(files):
            # Runner artifacts must never manufacture an apparent agent diff,
            # including when the repository does not ignore .orcexe/ yet.
            return [name for name in files if not (
                root / name == directory or directory in (root / name).parents)]

        before_head = gitutil.head(req.workdir)
        before_dirty = repository_files(gitutil.dirty(req.workdir))
        context = self._context.value
        status, warnings = "blocked", list(context["warnings"])
        observed = None
        extracted = {"final_text": "", "usage": {}, "model_actual": None}
        detected = {"version": None}
        # Always create the evidence artifacts, even for a refused write.
        paths = {key: str(directory / name) for key, name in _ARTIFACTS.items()}
        fsio.atomic_write_text(directory / "brief.md", Path(req.brief_path).read_text(encoding="utf-8"))
        fsio.atomic_write_text(paths["events_path"], "", mode=0o600)
        if context["preflight"]:
            status = context["preflight"]
        elif (req.profile == "write" and root ==
                Path(gitutil.main_worktree(req.workdir))):
            warnings.append("main_worktree_write_refused")
        else:
            self._ensure_brief_readable(req)
            prompt = "Read the task brief at {} and carry it out. Follow its rules exactly.".format(
                directory / "brief.md")
            _require(len(prompt) <= 600 and not prompt.startswith("-"), "brief pointer too long")
            values = dict(workdir=str(Path(req.workdir).resolve()), run_dir=str(directory),
                model=context["slug"], effort=req.effort, final=paths["final_text_path"], prompt=prompt)
            argv = self.manifest.build_argv(req.profile, **{
                name: values[name] for name in self.manifest.placeholders(req.profile)})
            detected = self.detect()
            # A missing binary must produce a spawn_error, never fall back to a
            # different CLI when an explicit binary override was requested.
            argv[0] = detected["path"] or self._env.get(
                "ORCEXE_BIN_" + req.tool.upper().replace("-", "_"), argv[0])
            recent, counts = deque(maxlen=32), Counter()
            shell_phases = Counter()

            def on_line(line):
                try:
                    event = json.loads(line, parse_constant=lambda _: None)
                except (ValueError, RecursionError):
                    event = {"_raw": line}
                if isinstance(event, dict) and event.get("type") == "thinking":
                    return
                event = redact.redact_obj(event)
                self._observe(event)
                if (isinstance(event, dict) and event.get('type') == 'tool_call' and
                        isinstance(event.get('shellToolCall'), dict)):
                    phase = event.get('subtype')
                    shell_phases[phase if phase in ('started', 'completed') else 'single'] += 1
                fsio.append_jsonl(paths["events_path"], event)
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
                config = self._config_path()
                snapshot = self._snapshot_model(config, warnings)
                try:
                    observed = proc.run(argv, cwd=str(req.workdir), env=self._environment(),
                        timeout_s=req.timeout_s, stall_s=req.stall_s, on_line=on_line,
                        cancel_event=cancel_event, kill_grace_s=self._kill_grace_s)
                finally:
                    self._restore_model(config, snapshot, warnings)
            finally:
                with self._lock:
                    del self._active[req.run_id]
            extracted = self._extract(list(recent))
            # Started/completed are two observations of one call. Include an
            # unfinished call on cancellation and completed-only stream shapes.
            shell_calls = max(shell_phases['started'], shell_phases['completed']) + shell_phases['single']
            runner_status = {"timeout": "timeout", "stalled": "stalled",
                             "cancelled": "cancelled", "spawn_error": "failed"}
            if observed.status in runner_status:
                status = runner_status[observed.status]
            else:
                status, extra = self._classify({"events": list(recent), "counts": dict(counts),
                                               "shell_calls": shell_calls}, observed)
                warnings.extend(extra)
                if status == "completed" and observed.returncode != 0:
                    status = "failed"
            if observed.pipes_held:
                warnings.append("pipes_held")
            if shell_calls:
                warnings.append('shell_used:{}'.format(shell_calls))
        after = gitutil.changed_since(req.workdir, before_head, before_dirty)
        after["diff_files"] = repository_files(after["diff_files"])
        after["untracked"] = repository_files(after["untracked"])
        if before_head != after["head"]:
            warnings.append("head_changed")
        if req.profile == "write" and not after["diff_files"]:
            warnings.append("empty_diff")
        actual = extracted.get("model_actual")
        if actual is not None and actual != context["slug"]:
            warnings.append("model_mismatch")
        final_text = redact.redact_text(extracted.get("final_text") or "")
        # Tool-written final files are not trusted as a second status channel.
        fsio.atomic_write_text(paths["final_text_path"], final_text, mode=0o600)
        fsio.atomic_write_text(paths["stderr_tail_path"],
            redact.redact_text(observed.stderr_tail if observed else ""), mode=0o600)
        final_line = final_text.rstrip("\r\n").split("\n")[-1]
        match = re.fullmatch(r"ORCEXE_EXIT: (DONE|DONE_WITH_CONCERNS|BLOCKED|NEEDS_CONTEXT)", final_line)
        code = observed.returncode if observed else None
        result = dict(schema="orcexe.run.v1", run_id=req.run_id, task=req.task, tool=req.tool,
            tool_version=detected["version"], family=self.manifest.family,
            model_requested=context["slug"], model_actual=actual, effort=req.effort, profile=req.profile,
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
        fsio.atomic_write_json(directory / "result.json", result, mode=0o600)
        return result
