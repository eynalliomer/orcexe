"""Repository risk areas and concrete-path policy shared by plan and task."""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from orcexe import classify, fsio, globs


@dataclass(frozen=True)
class RiskArea:
    pattern: Optional[str]
    basis: str
    lane: str = 'risky'


def repository_areas(repo=None, project=None):
    """Return all additive areas with their existing lane-basis labels.

    An unavailable input is represented explicitly, so consumers cannot silently
    narrow policy. Invalid local YAML retains the standard planning floor;
    concrete-path checks fail closed for any unavailable area.
    """
    areas = [RiskArea(p, 'cli:risk_path:' + p) for p in classify._RISK_PATHS]
    try:
        local = classify.repository_risk_map(repo) if repo is not None else classify.RiskMap()
        areas.extend(RiskArea(p, 'cli:risk_path:' + p) for p in local.risky_paths)
    except classify._RISK_MAP_ERRORS:
        areas.append(RiskArea(None, 'cli:risk_map_invalid:unavailable', 'standard'))
    try:
        if project is None:
            try:
                project = fsio.read_json(Path(repo) / '.orcexe/project-map.json') if repo is not None else {}
            except FileNotFoundError:
                if (Path(repo) / '.orcexe/project-map.json').is_symlink():
                    raise ValueError('project_map_invalid')
                project = {}
        for section in ('risk_map', 'do_not_touch'):
            entries = project.get(section, [])
            if not isinstance(entries, list):
                raise ValueError('project_map_invalid')
            for item in entries:
                pattern = item['path_glob']
                # Preserve conservative handling even for unusable non-risk classes.
                try:
                    globs.expand(pattern)
                except (ValueError, RecursionError):
                    areas.append(RiskArea(None, 'cli:risk_glob_unusable:' + pattern))
                    continue
                if section == 'risk_map' and item['class'] not in ('auth', 'payment', 'data', 'migration'):
                    continue
                basis = 'cli:' + section + ':' + (item['class'] + ':' if section == 'risk_map' else '')
                areas.append(RiskArea(pattern, basis + pattern))
    except (ValueError, OSError, TypeError, KeyError, AttributeError, RecursionError):
        areas.append(RiskArea(None, 'cli:risk_map_invalid:unavailable', 'risky'))
    return areas


def matches(path, pattern):
    """A concrete path inherits risk from its ancestors, including wildcard dirs."""
    parts = path.rstrip('/').split('/')
    return any(globs.matches('/'.join(parts[:i]), pattern) for i in range(1, len(parts) + 1))


def scope_touches(allowed, area, tracked=(), directories=()):
    """Judge any-depth areas by literal scope paths and covered tracked files."""
    for pattern in allowed:
        if any(matches(path, area.pattern) for path in globs.expand(pattern)):
            return True
        if any(globs.matches(path, pattern) and matches(path, area.pattern) for path in tracked):
            return True
        for risk in globs.expand(area.pattern):
            if not (risk == '**' or risk.startswith('**/')):
                if globs.overlaps(pattern, risk, directories):
                    return True
                if area.basis.startswith('cli:risk_path:') and globs.overlaps(pattern, risk + '/**', directories):
                    return True
    return False


def touched_paths(repo, paths):
    """Return risk-matching changed paths; unavailable policy never grants access."""
    areas = repository_areas(repo)
    found = []
    for path in sorted(set(paths)):
        try:
            touched = any(area.pattern is None or matches(path, area.pattern) for area in areas)
        except (ValueError, RecursionError):
            touched = True
        if touched:
            found.append(path)
    return found
