"""Prior-based routing (orcexe.roles.v1 / orcexe.route.v1), without model calls.

Walk stage.lane in shipped order, falling back to stage.any only when that
lane is absent. Reject, in order: absent tool, missing/unknown auth, excluded
family, denied model, model absent from a seen live list, exhausted quota,
and tool outside the user allow-list. An unseen list permits the prior with
low confidence; a seen empty list permits nothing. No arbitrary fallback.

Efforts rank low < medium < high < xhigh < max < ultra. Prefer the requested
effort, otherwise the nearest supported lower effort, otherwise the nearest
higher effort. Empty effort metadata is unknown and leaves the request intact.
Force replaces the candidate list, but retains every filter and effort rule.
Only gather_access probes tools; route reuses that snapshot for every stage.
"""

from dataclasses import dataclass
from datetime import date
import math
from pathlib import Path
import re
from types import MappingProxyType
from typing import Dict, Mapping, Optional, Tuple

from orcexe import miniyaml
from orcexe.manifest import Manifest


_EFFORTS = ("low", "medium", "high", "xhigh", "max", "ultra")
_STAGES = ("executor", "evaluator", "auditor")
_LANES = ("fast", "standard", "risky", "any")
_SKIPS = ("tool_absent", "auth_missing", "auth_unknown", "family_excluded",
          "model_denied", "model_unavailable", "quota_exhausted", "tool_not_allowed")
_FAILURES = ("no_prior", "no_eligible_candidate", "forced_unavailable")
_DEFAULT_ROLES = Path(__file__).resolve().parents[2] / "data/experience/roles.yaml"


@dataclass(frozen=True)
class Access:
    tool: str
    family: str
    present: bool
    auth: str
    models: Optional[Dict[str, Tuple[str, ...]]]
    quota_allowed: Optional[bool]
    quota_used_pct: Optional[float]


@dataclass(frozen=True)
class _Candidate:
    tool: str
    model: str
    effort: str


@dataclass(frozen=True)
class Roles:
    """Validated experience pack, with recursively read-only stage priors."""

    version: int
    as_of: str
    evidence: str
    stages: Mapping[str, Mapping[str, Tuple[_Candidate, ...]]]


def _fail(key, message):
    raise ValueError("{}: {}".format(key, message))


def _mapping(value, key, required=(), optional=()):
    if not isinstance(value, dict):
        _fail(key, "expected a mapping")
    for name in value:
        if name not in required and name not in optional:
            _fail(key + "." + str(name), "unknown key")
    for name in required:
        if name not in value:
            _fail(key + "." + name, "required key is missing")


def _text(value, key):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        _fail(key, "expected a nonempty string without control characters")


def _choice(value, key, choices):
    if not isinstance(value, str) or value not in choices:
        _fail(key, "expected one of " + ", ".join(choices))


def _date(value, key):
    _text(value, key)
    if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        _fail(key, "expected YYYY-MM-DD")
    try:
        date.fromisoformat(value)
    except ValueError:
        _fail(key, "invalid calendar date")


def _candidate(value, key):
    _mapping(value, key, ("tool", "model", "effort"))
    for name in ("tool", "model"):
        _text(value[name], key + "." + name)
    _choice(value["effort"], key + ".effort", _EFFORTS)
    return _Candidate(**value)


def load_roles(path=None) -> Roles:
    """Load the closed version:1 YAML schema; syntax errors name <root>."""
    try:
        data = miniyaml.load_path(_DEFAULT_ROLES if path is None else path)
    except miniyaml.MiniYamlError as exc:
        _fail("<root>", str(exc))
    _mapping(data, "roles", ("version", "as_of", "evidence", "stages"))
    if type(data["version"]) is not int or data["version"] != 1:
        _fail("version", "expected 1 (orcexe.roles.v1)")
    _date(data["as_of"], "as_of")
    _text(data["evidence"], "evidence")
    _mapping(data["stages"], "stages", optional=_STAGES)
    if not data["stages"]:
        _fail("stages", "expected at least one stage")
    stages = {}
    for stage, lanes in data["stages"].items():
        key = "stages." + stage
        _mapping(lanes, key, optional=_LANES)
        if not lanes:
            _fail(key, "expected at least one lane")
        parsed = {}
        for lane, candidates in lanes.items():
            location = key + "." + lane
            if not isinstance(candidates, list) or not candidates:
                _fail(location, "expected a nonempty candidate list")
            parsed[lane] = tuple(_candidate(item, "{}[{}]".format(location, index))
                                 for index, item in enumerate(candidates))
        stages[stage] = MappingProxyType(parsed)
    return Roles(data["version"], data["as_of"], data["evidence"], MappingProxyType(stages))


def _probe(adapter, name):
    try:
        value = getattr(adapter, name)()
        return value if isinstance(value, dict) else {}
    except Exception:
        # Probe errors may contain account data. Retain no error text.
        return {}


def _models(probe):
    if probe.get("source", "none") == "none" or not isinstance(probe.get("models"), list):
        return None
    models = {}
    for item in probe["models"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not item["id"]:
            return None
        efforts = item.get("supported_efforts", ())
        if not isinstance(efforts, (list, tuple)):
            efforts = ()
        models[item["id"]] = tuple(e for e in _EFFORTS if e in efforts)
    return models


def _quota(probe):
    if probe.get("source", "none") == "none":
        return None, None
    allowed = probe.get("allowed")
    allowed = allowed if type(allowed) is bool else None
    windows = probe.get("windows", [])
    percentages = []
    for window in windows if isinstance(windows, list) else []:
        value = window.get("used_pct") if isinstance(window, dict) else None
        if type(value) in (int, float) and math.isfinite(value) and value >= 0:
            percentages.append(float(value))
    return allowed, max(percentages) if percentages else None


def gather_access(adapters) -> list[Access]:
    """Probe each adapter once per method, even after a failure; never run it.

    Identity comes only from the validated Manifest loaded by load_manifest
    when constructing the adapter. No binary path, plan, account, reset time,
    raw response, or diagnostic is copied into the access snapshot.
    """
    result = []
    for adapter in adapters:
        try:
            manifest = adapter.manifest
            tool, family = (manifest.tool, manifest.family) if isinstance(manifest, Manifest) else ("", "")
        except Exception:
            tool, family = "", ""
        detected = _probe(adapter, "detect")
        auth = _probe(adapter, "auth_status")
        models = _probe(adapter, "list_models")
        quota = _probe(adapter, "quota")
        try:
            state = auth.get("state")
            state = state if state in ("ok", "missing", "unknown") else "unknown"
            allowed, used = _quota(quota)
            result.append(Access(tool, family, bool(tool) and detected.get("present") is True,
                                 state, _models(models), allowed, used))
        except Exception:
            result.append(Access(tool, family, False, "unknown", None, None, None))
    return result


def _constraints(value):
    value = {} if value is None else value
    _mapping(value, "constraints", optional=("tools", "deny_models", "force", "exclude_families"))
    for key in ("tools", "deny_models", "exclude_families"):
        if key in value:
            if not isinstance(value[key], (list, tuple, set, frozenset)):
                _fail("constraints." + key, "expected a collection of strings")
            for item in value[key]:
                _text(item, "constraints." + key)
    forced = _candidate(value["force"], "constraints.force") if "force" in value else None
    return value, forced


def _skip(candidate, access, constraints):
    if access is None or not access.present:
        return "tool_absent"
    if access.auth != "ok":
        return "auth_missing" if access.auth == "missing" else "auth_unknown"
    if access.family in constraints.get("exclude_families", ()):
        return "family_excluded"
    if candidate.model in constraints.get("deny_models", ()):
        return "model_denied"
    if access.models is not None and candidate.model not in access.models:
        return "model_unavailable"
    if access.quota_allowed is False or access.quota_used_pct is not None and access.quota_used_pct >= 95:
        return "quota_exhausted"
    if "tools" in constraints and candidate.tool not in constraints["tools"]:
        return "tool_not_allowed"
    return None


def _effort(requested, supported):
    supported = tuple(e for e in _EFFORTS if e in supported)
    if not supported or requested in supported:
        return requested
    lower = [e for e in supported if _EFFORTS.index(e) < _EFFORTS.index(requested)]
    return lower[-1] if lower else supported[0]


def route(stage, lane, access, roles, *, constraints=None) -> dict:
    """Choose deterministically from a reusable access snapshot, or fail openly."""
    _choice(stage, "stage", _STAGES)
    _choice(lane, "lane", _LANES)
    constraints, forced = _constraints(constraints)
    lanes = roles.stages.get(stage, {})
    prior_lane = lane if lane in lanes else "any"
    candidates = (forced,) if forced else lanes.get(prior_lane, ())
    # One snapshot per tool is required; duplicates must not choose an arbitrary
    # view of access. This also makes adapter iteration order immaterial.
    by_tool = {}
    for item in access:
        if item.tool in by_tool:
            _fail("access." + item.tool, "duplicate tool")
        by_tool[item.tool] = item
    result = dict(schema="orcexe.route.v1", stage=stage, lane=lane, tool=None,
                  family=None, model=None, effort=None, confidence="low",
                  basis=[], skipped=[], roles_as_of=roles.as_of)
    for index, candidate in enumerate(candidates):
        origin = ("constraint:force" if forced else
                  "prior:roles.yaml#{}.{}[{}]".format(stage, prior_lane, index))
        result["basis"].append(origin)
        observed = by_tool.get(candidate.tool)
        reason = _skip(candidate, observed, constraints)
        if reason:
            name = candidate.tool + "/" + candidate.model
            result["skipped"].append(dict(candidate=name, reason=reason))
            result["basis"].append("skip:" + name + ":" + reason)
            continue
        live = observed.models is not None
        result["basis"].append("access:live" if live else "access:not_seen")
        used = observed.quota_used_pct
        result["basis"].append("quota:not_seen" if used is None else "quota:ok({:g}%)".format(used))
        effort = _effort(candidate.effort, observed.models[candidate.model] if live else ())
        if effort != candidate.effort:
            result["basis"].append("effort_adjusted:{}->{}".format(candidate.effort, effort))
        result.update(tool=candidate.tool, family=observed.family, model=candidate.model,
                      effort=effort, confidence="normal" if live else "low")
        break
    if result["tool"] is None:
        reason = "forced_unavailable" if forced else "no_eligible_candidate" if candidates else "no_prior"
        result["reason"] = reason
        result["basis"].append("reason:" + reason)
    validate_route(result)
    return result


def validate_route(data) -> None:
    """Reject unknown fields, invalid types, and inconsistent null selections."""
    _mapping(data, "route", ("schema", "stage", "lane", "tool", "family", "model", "effort",
                             "confidence", "basis", "skipped", "roles_as_of"), ("reason",))
    _choice(data["schema"], "schema", ("orcexe.route.v1",))
    _choice(data["stage"], "stage", _STAGES)
    _choice(data["lane"], "lane", _LANES)
    _choice(data["confidence"], "confidence", ("normal", "low"))
    _date(data["roles_as_of"], "roles_as_of")
    if not isinstance(data["basis"], list) or not data["basis"]:
        _fail("basis", "expected a nonempty list")
    for index, item in enumerate(data["basis"]):
        _text(item, "basis[{}]".format(index))
    if not isinstance(data["skipped"], list):
        _fail("skipped", "expected a list")
    for index, item in enumerate(data["skipped"]):
        key = "skipped[{}]".format(index)
        _mapping(item, key, ("candidate", "reason"))
        _text(item["candidate"], key + ".candidate")
        _choice(item["reason"], key + ".reason", _SKIPS)
    if data["tool"] is None:
        for key in ("family", "model", "effort"):
            if data[key] is not None:
                _fail(key, "must be null without a tool")
        _choice(data.get("reason"), "reason", _FAILURES)
    else:
        for key in ("tool", "family", "model"):
            _text(data[key], key)
        _choice(data["effort"], "effort", _EFFORTS)
        if "reason" in data:
            _fail("reason", "only allowed without a tool")
    if (data["tool"] is None or "access:not_seen" in data["basis"]) and data["confidence"] != "low":
        _fail("confidence", "must be low without observed model access")
