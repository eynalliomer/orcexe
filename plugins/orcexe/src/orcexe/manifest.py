"""Strict v1 capability manifests; validation reports the first error.

Only data is loaded: install text and command descriptions are never executed.
Evidence prefixes use dot boundaries and omit list indices. Unknown evidence
resolves to ``?``. Review support is deliberately closed: until a tool has a
code rule, its read-only assertion cannot be trusted.
"""

from dataclasses import dataclass
import os
import re
from types import MappingProxyType

from orcexe.miniyaml import MiniYamlError, load_path


# A trailing * denotes a prefix, not a shell glob. Manifests cannot remove these.
GLOBAL_FORBIDDEN_ARGV = (
    "--yolo", "--force", "danger-full-access", "bypassPermissions", "--bare",
    "--full-auto", "--dangerously-*",
)
_PLACEHOLDERS = frozenset(("workdir", "run_dir", "model", "effort", "final", "prompt"))
_REQUIRED_PLACEHOLDERS = ("workdir", "model", "prompt")
_FAMILIES = {"codex": "openai", "claude-code": "anthropic", "cursor": "cursor"}
_REVIEW_RULES = {"codex": ("-s", "read-only"), "claude-code": ("--permission-mode", "plan"),
                 "cursor": ("--mode", "plan")}
_TAGS = ("D", "Y", "D+Y", "?")
_SOURCES = ("app-server", "stream", "cmd", "none")
# Command/version/install facts and uncertain exit codes also carry evidence;
# schema/tool/family/bin_env are manifest identity and configuration metadata.
_COVERED = ("auth", "models", "quota", "exec", "events", "side_effects", "windows",
            "binary", "min_version", "version_cmd", "version_regex",
            "version_sample", "verified_on", "install", "exit_codes")
_TOP = (
    "schema", "tool", "family", "binary", "bin_env", "min_version", "version_cmd",
    "version_regex", "version_sample", "verified_on", "install", "auth", "models",
    "quota", "exec", "events", "exit_codes", "side_effects", "windows", "evidence",
)
_VERSION = re.compile(
    r"(?<![\w.])([0-9]+(?:\.[0-9]+)+)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?![\w.+-])"
)


class ManifestError(ValueError):
    """Invalid data at ``path``; ``message`` excludes the path prefix."""

    def __init__(self, path, message):
        self.path = path
        self.message = message
        super().__init__("{}: {}".format(path, message))


def _fail(path, message):
    raise ManifestError(path, message)


def _version_parts(text, path="version"):
    if not isinstance(text, str):
        _fail(path, "expected a version string")
    match = _VERSION.search(text)
    if match is None:
        _fail(path, "unparseable version")
    try:
        numbers = tuple(int(part) for part in match[1].split("."))
        pre = None if match[2] is None else tuple(
            (0, int(part)) if part.isdigit() else (1, part)
            for part in match[2].split(".")
        )
    except ValueError:
        _fail(path, "version component is too large")
    return numbers, pre


def parse_version(text) -> tuple:
    """Extract numeric release components, also accepting CLI version output.

    Pre-release precedence is handled by meets_min_version, without changing
    this public numeric tuple representation. Build metadata is ignored.
    """
    return _version_parts(text)[0]


def meets_min_version(found: str, minimum: str) -> bool:
    """Compare numeric components (zero padded), then SemVer pre-releases."""
    left, left_pre = _version_parts(found)
    right, right_pre = _version_parts(minimum)
    size = max(len(left), len(right))
    left += (0,) * (size - len(left))
    right += (0,) * (size - len(right))
    if left != right:
        return left > right
    if left_pre is None:
        return True
    if right_pre is None:
        return False
    return left_pre >= right_pre


def _string(value, path):
    if not isinstance(value, str) or not value or "\0" in value:
        _fail(path, "expected a non-empty string without NUL")


def _boolean(value, path):
    if type(value) is not bool:
        _fail(path, "expected a boolean")


def _integer(value, path):
    if type(value) is not int or value < 0:
        _fail(path, "expected a non-negative integer")


def _mapping(value, path, required=(), optional=()):
    if not isinstance(value, dict):
        _fail(path, "expected a mapping")
    for key in value:
        if key not in required and key not in optional:
            _fail(_join(path, str(key)), "unknown key")
    for key in required:
        if key not in value:
            _fail(_join(path, key), "required key is missing")


def _join(path, key):
    return path + "." + key if path else key


def _strings(value, path, nonempty=False):
    if not isinstance(value, list) or nonempty and not value:
        _fail(path, "expected {}list of strings".format("a non-empty " if nonempty else "a "))
    for index, item in enumerate(value):
        _string(item, "{}[{}]".format(path, index))


def _choice(value, path, choices):
    if not isinstance(value, str) or value not in choices:
        _fail(path, "expected one of {}".format(", ".join(choices)))


def _string_map(value, path):
    if not isinstance(value, dict):
        _fail(path, "expected a string-to-string mapping")
    for key, item in value.items():
        _string(key, path)
        _string(item, _join(path, key))


def _safe_tree(value, path="", ancestors=()):
    """Bound direct validate() inputs too, including cycles and exotic objects."""
    if len(ancestors) > 64:
        _fail(path, "nesting depth exceeds 64")
    if isinstance(value, (dict, list)):
        if id(value) in ancestors:
            _fail(path, "cyclic data")
        ancestors += (id(value),)
        items = value.items() if isinstance(value, dict) else enumerate(value)
        for key, item in items:
            if isinstance(value, dict):
                _string(key, path or "<root>")
                child = _join(path, key)
            else:
                child = "{}[{}]".format(path, key)
            _safe_tree(item, child, ancestors)
    elif value is not None and type(value) not in (str, bool, int, float):
        _fail(path, "expected YAML data")


def _placeholders(token, path):
    names = re.findall(r"\{([^{}]*)\}", token)
    for name in names:
        if name not in _PLACEHOLDERS:
            _fail(path, "unknown placeholder {{{}}}".format(name))
    remainder = re.sub(r"\{[^{}]*\}", "", token)
    if "{" in remainder or "}" in remainder:
        _fail(path, "malformed placeholder")
    return names


def _check_forbidden(token, path, forbidden):
    # Inspect whitespace-delimited words and all '=' values, including quoted
    # -c values. This is inspection only: no shell parsing or execution occurs.
    token = token.casefold()
    forbidden = tuple(denied.casefold() for denied in forbidden)
    parts = [token] + re.split(r"[\s='\";|&()]+", token)
    # Local denials may themselves contain '='. Check them without building
    # every assignment suffix (which would allocate quadratically).
    boundary = r"[\s='\";|&()]"
    for denied in forbidden:
        if "=" in denied:
            literal = denied[:-1] if denied.endswith("*") else denied
            pattern = r"(?:^|" + boundary + ")" + re.escape(literal)
            if not denied.endswith("*"):
                pattern += r"(?=$|" + boundary + ")"
            if re.search(pattern, token):
                _fail(path, "forbidden argv value {!r}".format(denied))
    for part in parts:
        for denied in forbidden:
            if part == denied or denied.endswith("*") and part.startswith(denied[:-1]):
                _fail(path, "forbidden argv token {!r}".format(part))


def _condition(value, path):
    if not isinstance(value, dict):
        _fail(path, "expected a login condition mapping")
    if "exit_code" in value or not any(key in value for key in ("json_field", "equals")):
        _mapping(value, path, ("exit_code",))
        _integer(value["exit_code"], path + ".exit_code")
    else:
        _mapping(value, path, ("json_field", "equals"))
        _string(value["json_field"], path + ".json_field")
        if isinstance(value["equals"], (dict, list)):
            _fail(path + ".equals", "expected a scalar")


def _source(value, path, required=(), optional=()):
    _mapping(value, path, ("source",) + required,
             ("method", "cmd", "parse", "event") + optional)
    source = value["source"]
    _choice(source, path + ".source", _SOURCES)
    transport = {"app-server": "method", "cmd": "cmd", "stream": "event"}.get(source)
    if transport is not None and transport not in value:
        _fail(path + "." + transport, "required for source " + source)
    for key in ("method", "cmd", "parse", "event"):
        if key not in value:
            continue
        if key != transport and not (key == "parse" and source == "cmd"):
            _fail(path + "." + key, "inconsistent with source " + source)
        if key == "cmd":
            _strings(value[key], path + ".cmd", True)
        else:
            _string(value[key], path + "." + key)


def _review(data, tokens):
    path = "exec.profiles.review"
    if data["exec"]["profiles"]["review"]["read_only"] is not True:
        _fail(path + ".read_only", "review must assert read_only: true")
    rule = _REVIEW_RULES.get(data["tool"])
    if rule is None:
        _fail(path, "no verified read-only rule for this tool")
    for token, location in tokens:
        if data["tool"] == "cursor" and token.startswith("--mode="):
            _fail(location, "attached mode would override the review mode")
        # Config overrides and alternate/attached sandbox options must not
        # defeat the verified -s pair. Reject them even if an -s pair follows.
        if (re.search(r"sandbox_mode[\"']?\s*=", token) or
                token.startswith(("--sandbox", "-s=")) or
                token.startswith("-s") and token != "-s"):
            _fail(location, "unverified sandbox override in review argv")
    positions = [i for i, (token, _) in enumerate(tokens) if token == rule[0]]
    if len(positions) != 1:
        _fail(tokens[positions[-1]][1] if len(positions) > 1 else (path if data["tool"] == "cursor" else path + ".extra"),
              "review requires exactly one {} {}".format(*rule))
    position = positions[0]
    if position + 1 >= len(tokens) or tokens[position + 1][0] != rule[1]:
        location = tokens[min(position + 1, len(tokens) - 1)][1]
        _fail(location, "review sandbox must be " + rule[1])
    # The selector must actually be an option, rather than a positional value
    # after '--' or the argument of another documented Codex option.
    index = 0
    while index < position:
        token, location = tokens[index]
        if token == "--":
            _fail(location, "option terminator would disable the review sandbox")
        if token in ("-C", "-m", "-c", "-o"):
            if index + 1 == position:
                _fail(location, "option would consume the review sandbox flag")
            index += 2
        else:
            index += 1


def _paths(value, path):
    """Yield existing paths and leaves; list items share their parent's path."""
    yield path, not isinstance(value, (dict, list)) or not value, value
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _paths(item, _join(path, key))
    elif isinstance(value, list):
        for item in value:
            yield from _paths(item, path)


def _evidence_tag(evidence, path):
    while path:
        if path in evidence:
            return evidence[path]
        path = path.rpartition(".")[0]
    return "?"


def _validate_evidence(data):
    evidence = data["evidence"]
    if not isinstance(evidence, dict):
        _fail("evidence", "expected a mapping")
    existing = {path for path, _, _ in _paths(data, "")
                if path and path != "evidence" and not path.startswith("evidence.")}
    for path, tag in evidence.items():
        _choice(tag, "evidence." + path, _TAGS)
        if path not in existing:
            _fail("evidence." + path, "stale evidence: no such path")
    for root in _COVERED:
        for path, leaf, value in _paths(data[root], root):
            if leaf and not any(path == prefix or path.startswith(prefix + ".")
                                for prefix in evidence):
                _fail(path, "missing evidence coverage")
            # These null behaviours are unknown, even beneath a documented
            # prefix. A login condition's equals: null is a defined comparison.
            if leaf and value is None and path in ("quota.exec_exhaustion", "events.model_actual"):
                if _evidence_tag(evidence, path) != "?":
                    _fail(path, "null behaviour must have '?' evidence")


def validate(data: dict) -> None:
    """Reject the first schema, safety, consistency, or evidence problem.

    Dynamic install/evidence keys are intentional; all schema mappings are
    closed. List error locations have indices, but evidence paths do not.
    """
    _safe_tree(data)
    _mapping(data, "", _TOP)
    _choice(data["schema"], "schema", ("orcexe.manifest.v1",))
    _choice(data["tool"], "tool", tuple(_FAMILIES))
    _choice(data["family"], "family", (_FAMILIES[data["tool"]],))
    _strings(data["binary"], "binary", True)
    _string(data["bin_env"], "bin_env")
    if re.fullmatch(r"ORCEXE_BIN_[A-Z_]+", data["bin_env"]) is None:
        _fail("bin_env", "expected ORCEXE_BIN_[A-Z_]+")
    _version_parts(data["min_version"], "min_version")
    _strings(data["version_cmd"], "version_cmd", True)
    _string(data["version_regex"], "version_regex")
    _string(data["version_sample"], "version_sample")
    try:
        version_regex = re.compile(data["version_regex"])
    except (re.error, OverflowError, RecursionError) as exc:
        _fail("version_regex", "invalid regex: " + str(exc))
    if version_regex.groups != 1:
        _fail("version_regex", "expected exactly one capture group")
    sample = version_regex.search(data["version_sample"])
    if sample is None or sample[1] is None:
        _fail("version_sample", "does not match version_regex with a captured version")
    _version_parts(sample[1], "version_sample")
    _mapping(data["verified_on"], "verified_on", ("date", "version", "os"))
    for key, value in data["verified_on"].items():
        _string(value, "verified_on." + key)
    _version_parts(data["verified_on"]["version"], "verified_on.version")
    _string_map(data["install"], "install")

    auth = data["auth"]
    _mapping(auth, "auth", ("status_cmd", "logged_in", "logged_out", "login_cmd",
                            "device_cmd", "plan", "allowed_fields", "forbidden_fields"))
    for key in ("status_cmd", "login_cmd", "device_cmd"):
        _strings(auth[key], "auth." + key, True)
    for key in ("logged_in", "logged_out"):
        _condition(auth[key], "auth." + key)
    if auth["logged_in"] == auth["logged_out"]:
        _fail("auth.logged_out", "login conditions must differ")
    _source(auth["plan"], "auth.plan", ("field",))
    _string(auth["plan"]["field"], "auth.plan.field")
    for key in ("allowed_fields", "forbidden_fields"):
        _strings(auth[key], "auth." + key)
    for index, field in enumerate(auth["allowed_fields"]):
        if field in auth["forbidden_fields"]:
            _fail("auth.allowed_fields[{}]".format(index), "field is also forbidden")
    if auth["plan"]["field"] not in auth["allowed_fields"]:
        _fail("auth.plan.field", "plan field must be allowed")
    _source(data["models"], "models")
    quota = data["quota"]
    _source(quota, "quota", ("fields", "nullable_fields", "read_only_consumes_quota", "exec_exhaustion"))
    for key in ("fields", "nullable_fields"):
        _strings(quota[key], "quota." + key)
    for index, field in enumerate(quota["nullable_fields"]):
        if field not in quota["fields"]:
            _fail("quota.nullable_fields[{}]".format(index), "field is not in quota.fields")
    _boolean(quota["read_only_consumes_quota"], "quota.read_only_consumes_quota")
    if quota["exec_exhaustion"] is not None:
        _string(quota["exec_exhaustion"], "quota.exec_exhaustion")

    execution = data["exec"]
    _mapping(execution, "exec", ("argv", "stdin", "requires_git_repo", "effort", "profiles", "forbidden_argv"), ("workdir_via",))
    _choice(execution.get("workdir_via", "argv"), "exec.workdir_via", ("argv", "cwd"))
    _strings(execution["argv"], "exec.argv", True)
    _choice(execution["stdin"], "exec.stdin", ("devnull",))
    _boolean(execution["requires_git_repo"], "exec.requires_git_repo")
    _mapping(execution["effort"], "exec.effort", ("mode", "values", "model_dependent"))
    _choice(execution["effort"]["mode"], "exec.effort.mode", ("config", "flag", "slug"))
    _strings(execution["effort"]["values"], "exec.effort.values", True)
    _boolean(execution["effort"]["model_dependent"], "exec.effort.model_dependent")
    _strings(execution["forbidden_argv"], "exec.forbidden_argv")
    forbidden = GLOBAL_FORBIDDEN_ARGV + tuple(execution["forbidden_argv"])
    _mapping(execution["profiles"], "exec.profiles", ("write", "review"))
    base_names = []
    for index, token in enumerate(execution["argv"]):
        path = "exec.argv[{}]".format(index)
        _check_forbidden(token, path, forbidden)
        base_names.extend(_placeholders(token, path))
        for name in _REQUIRED_PLACEHOLDERS:
            if base_names.count(name) > 1:
                _fail(path, "duplicate required placeholder {{{}}}".format(name))
    for name in _REQUIRED_PLACEHOLDERS:
        if name == "workdir" and execution.get("workdir_via", "argv") == "cwd":
            continue
        if base_names.count(name) != 1:
            _fail("exec.argv", "expected exactly one {{{}}}".format(name))
    for profile, config in execution["profiles"].items():
        path = "exec.profiles." + profile
        _mapping(config, path, ("extra", "read_only") if profile == "review" else ("extra",),
                 () if profile == "review" else ("read_only",))
        if "read_only" in config:
            _boolean(config["read_only"], path + ".read_only")
        _strings(config["extra"], path + ".extra")
        names = list(base_names)
        for index, token in enumerate(config["extra"]):
            location = "{}.extra[{}]".format(path, index)
            _check_forbidden(token, location, forbidden)
            names.extend(_placeholders(token, location))
            if any(names.count(name) > 1 for name in _REQUIRED_PLACEHOLDERS):
                _fail(location, "duplicate required placeholder")
    _review(data, _argv_locations(execution, "review"))

    events = data["events"]
    _mapping(events, "events", ("format", "stream", "types", "success", "final_text", "usage", "model_actual", "error"))
    _choice(events["format"], "events.format", ("jsonl",))
    _choice(events["stream"], "events.stream", ("stdout",))
    _strings(events["types"], "events.types", True)
    _mapping(events["success"], "events.success", ("last_type", "absent_types"))
    _string(events["success"]["last_type"], "events.success.last_type")
    _strings(events["success"]["absent_types"], "events.success.absent_types")
    if events["success"]["last_type"] in events["success"]["absent_types"]:
        _fail("events.success.last_type", "success type cannot be absent")
    for name, keys in (("final_text", ("type", "item_type", "field")),
                       ("error", ("field", "format"))):
        _mapping(events[name], "events." + name, keys)
        for key in keys:
            _string(events[name][key], "events." + name + "." + key)
    _choice(events["error"]["format"], "events.error.format", ("text",))
    _mapping(events["usage"], "events.usage", ("type", "map"))
    _string(events["usage"]["type"], "events.usage.type")
    _mapping(events["usage"]["map"], "events.usage.map", ("input", "cached_read", "output", "reasoning"))
    for key, value in events["usage"]["map"].items():
        _string(value, "events.usage.map." + key)
    if events["model_actual"] is not None:
        _string(events["model_actual"], "events.model_actual")
    for key in ("last_type", "absent_types"):
        types = events["success"][key]
        for event in [types] if isinstance(types, str) else types:
            _known_event(event, events["types"], "events.success." + key)
    for key in ("final_text", "usage"):
        _known_event(events[key]["type"], events["types"], "events." + key + ".type")

    _mapping(data["exit_codes"], "exit_codes", ("usage", "runtime"))
    for key, value in data["exit_codes"].items():
        _integer(value, "exit_codes." + key)
    if not isinstance(data["side_effects"], list):
        _fail("side_effects", "expected a list")
    for index, effect in enumerate(data["side_effects"]):
        path = "side_effects[{}]".format(index)
        if isinstance(effect, dict) and effect.get("handler") == "restore-after-run":
            _mapping(effect, path, ("id", "file", "keys", "handler"))
            for key in ("id", "file", "handler"):
                _string(effect[key], path + "." + key)
            _strings(effect["keys"], path + ".keys", nonempty=True)
            continue
        _mapping(effect, path, ("id", "file", "table", "entry", "profile", "requires_git_repo", "config_override_prevents", "handler"))
        for key in ("id", "file", "table", "entry", "handler"):
            _string(effect[key], path + "." + key)
        _choice(effect["profile"], path + ".profile", ("write", "review"))
        for key in ("requires_git_repo", "config_override_prevents"):
            _boolean(effect[key], path + "." + key)
    _mapping(data["windows"], "windows", ("native", "sandbox_native", "wsl2", "wsl1"))
    for key, value in data["windows"].items():
        _boolean(value, "windows." + key)

    # Every command description is inspected, not only exec argv. Installation
    # strings are display text and intentionally excluded from argv inspection.
    commands = [("binary", data["binary"]), ("version_cmd", data["version_cmd"])]
    commands.extend(("auth." + key, auth[key]) for key in ("status_cmd", "login_cmd", "device_cmd"))
    commands.extend((root + ".cmd", obj["cmd"]) for root, obj in
                    (("auth.plan", auth["plan"]), ("models", data["models"]), ("quota", quota)) if "cmd" in obj)
    for path, command in commands:
        for index, token in enumerate(command):
            location = "{}[{}]".format(path, index)
            _check_forbidden(token, location, forbidden)
            if "{" in token or "}" in token:
                _fail(location, "placeholders are only supported in exec argv")
    _validate_evidence(data)


def _known_event(event, types, path):
    if not any(event == known or known.endswith(".*") and event.startswith(known[:-1]) for known in types):
        _fail(path, "event is not declared in events.types")


def _argv_locations(execution, profile):
    return [(token, "exec.argv[{}]".format(index)) for index, token in enumerate(execution["argv"])] + [
        (token, "exec.profiles.{}.extra[{}]".format(profile, index))
        for index, token in enumerate(execution["profiles"][profile]["extra"])
    ]


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


@dataclass(frozen=True, init=False)
class Manifest:
    """Validated, detached, recursively read-only manifest data."""

    data: object

    def __init__(self, data):
        validate(data)
        object.__setattr__(self, "data", _freeze(data))

    @property
    def tool(self):
        return self.data["tool"]

    @property
    def family(self):
        return self.data["family"]

    def evidence(self, path) -> str:
        """Resolve the longest dotted prefix; an uncovered path is unknown."""
        return _evidence_tag(self.data["evidence"], path)

    def is_uncertain(self, path) -> bool:
        return self.evidence(path) == "?"

    def placeholders(self, profile) -> frozenset:
        """Return the names used by this profile, without weakening substitution."""
        if not isinstance(profile, str) or profile not in self.data["exec"]["profiles"]:
            _fail("exec.profiles." + str(profile), "unknown profile")
        return frozenset(name for token, path in _argv_locations(self.data["exec"], profile)
                         for name in _placeholders(token, path))

    def build_argv(self, profile, **values) -> list[str]:
        """Substitute values as argv data, with rules for each placeholder kind.

        Prompts are capped at 8,000 characters to leave room under Windows'
        roughly 32K whole-command-line limit. The adapter will pass the brief
        itself as a file; callers must not put an unbounded brief in argv.
        """
        execution = self.data["exec"]
        if not isinstance(profile, str) or profile not in execution["profiles"]:
            _fail("exec.profiles." + str(profile), "unknown profile")
        tokens = _argv_locations(execution, profile)
        needed = {name for token, path in tokens for name in _placeholders(token, path)}
        for name in sorted(needed - values.keys()):
            _fail("exec.argv", "missing placeholder value: " + name)
        for name in sorted(values.keys() - needed):
            _fail("exec.argv." + name, "unexpected placeholder value")
        forbidden = GLOBAL_FORBIDDEN_ARGV + execution["forbidden_argv"]
        for name, value in values.items():
            path = "exec.argv." + name
            _string(value, path)
            if value.lstrip().startswith("-"):
                _fail(path, "placeholder value must not start with '-'")
            if name == "prompt":
                if len(value) > 8000:
                    _fail(path, "prompt exceeds 8,000 characters")
            elif name in ("workdir", "run_dir", "final"):
                if not os.path.isabs(value):
                    _fail(path, "expected an absolute path")
                if name == "run_dir" and not os.path.isdir(value):
                    _fail(path, "expected an existing directory")
            else:
                if any(c.isspace() for c in value):
                    _fail(path, "model and effort must be single values without whitespace")
                _check_forbidden(value, path, forbidden)
            if name == "effort" and value not in execution["effort"]["values"]:
                _fail(path, "unsupported effort")
        result = []
        checked = []
        for token, path in tokens:
            # A callback avoids recursively substituting braces in supplied text.
            rendered = re.sub(r"\{([^{}]*)\}", lambda match: values[match[1]], token)
            # Keep free text and paths opaque to argv safety checks, including
            # review checks. Still inspect literals and model/effort expansions
            # for forbidden tokens synthesized across a template boundary.
            inspected = re.sub(r"\{([^{}]*)\}", lambda match: (
                values[match[1]] if match[1] in ("model", "effort") else match[0]
            ), token)
            _check_forbidden(inspected, path, forbidden)
            result.append(rendered)
            checked.append((inspected, path))
        if profile == "review":
            _review(self.data, checked)
        return result


def load_manifest(path) -> Manifest:
    """Read via miniyaml, wrap syntax errors at <root>, validate and freeze.

    Filesystem errors retain their usual OSError types, as in miniyaml.load_path.
    """
    try:
        data = load_path(path)
    except MiniYamlError as exc:
        raise ManifestError("<root>", str(exc)) from exc
    return Manifest(data)
