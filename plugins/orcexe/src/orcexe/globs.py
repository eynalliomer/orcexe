"""Shared bounded glob grammar for maps, questions and plans.

* / ? / classes match within a segment; ** is a whole segment matching zero
or more segments. Flat {a,b} alternatives may include slashes and wildcards.
Expansion and matching budgets are deterministic, independent of wall time.
"""

from functools import lru_cache
import re
import os
from pathlib import Path
import unicodedata

EXPANSION_CAP = 256
EXPANDED_TEXT_CAP = 4096
WORK_CAP = 100000


def _segment(pattern):
    """Glob tokens as inclusive Unicode intervals; None is the star loop."""
    tokens, i = [], 0
    while i < len(pattern):
        char = pattern[i]
        i += 1
        if char == '*':
            tokens.append(None)
        elif char == '?':
            tokens.append(((1, 0x10ffff),))
        elif char == '[':
            start = i + int(pattern[i:i + 1] == '!')
            start += int(pattern[start:start + 1] == ']')
            end = pattern.find(']', start)
            if end < 0:
                raise ValueError('invalid_glob')
            body = pattern[i:end]
            negate = body.startswith('!')
            body = body[1:] if negate else body
            if not body:
                raise ValueError('invalid_glob')
            ranges, j = [], 0
            while j < len(body):
                low = high = ord(body[j])
                if j + 2 < len(body) and body[j + 1] == '-':
                    high = ord(body[j + 2])
                    if high < low:
                        raise ValueError('invalid_glob')
                    j += 2
                ranges.append((low, high))
                j += 1
            ranges.sort()
            if negate:
                complement, start = [], 1
                for low, high in ranges:
                    if start < low:
                        complement.append((start, low - 1))
                    start = max(start, high + 1)
                if start <= 0x10ffff:
                    complement.append((start, 0x10ffff))
                ranges = complement
            tokens.append(tuple(ranges))
            i = end + 1
        elif char == ']':
            raise ValueError('invalid_glob')
        else:
            tokens.append(((ord(char), ord(char)),))
    return tokens


def _expand_braces(value):
    """Flat alternatives may contain slashes and wildcards; classes are opaque."""
    result, literal, i = [''], '', 0
    while i < len(value):
        char = value[i]
        if char == '[':
            start = i + 1 + int(value[i + 1:i + 2] == '!')
            start += int(value[start:start + 1] == ']')
            end = value.find(']', start)
            if end < 0:
                raise ValueError('invalid_glob')
            literal += value[i:end + 1]
            i = end + 1
            continue
        if char == '}':
            raise ValueError('invalid_glob')
        if char != '{':
            literal += char
            i += 1
            continue
        choices, choice, i = [], '', i + 1
        while i < len(value) and value[i] != '}':
            if value[i] == '{':
                raise ValueError('invalid_glob')
            if value[i] == '[':
                start = i + 1 + int(value[i + 1:i + 2] == '!')
                start += int(value[start:start + 1] == ']')
                end = value.find(']', start)
                if end < 0:
                    raise ValueError('invalid_glob')
                choice += value[i:end + 1]
                i = end + 1
                continue
            if value[i] == ',':
                choices.append(choice)
                choice = ''
            else:
                choice += value[i]
            i += 1
        choices.append(choice)
        if i == len(value) or len(choices) < 2 or not all(choices):
            raise ValueError('invalid_glob')
        if (len(result) * len(choices) > EXPANSION_CAP or
                sum(len(p) + len(literal) + len(c) for p in result for c in choices) > EXPANDED_TEXT_CAP):
            raise ValueError('glob_expansion_limit')
        result = [p + literal + c for p in result for c in choices]
        literal, i = '', i + 1
    if sum(len(p) + len(literal) for p in result) > EXPANDED_TEXT_CAP:
        raise ValueError('glob_expansion_limit')
    return [p + literal for p in result]


def expand(pattern):
    """Validate grammar and bounds, independently of executor path policy."""
    if (not isinstance(pattern, str) or not pattern or len(pattern) > 1024
            or pattern.startswith('/') or re.match(r'^[A-Za-z]:', pattern) or '\\' in pattern
            or any(unicodedata.category(c) in ('Cc', 'Cf') for c in pattern)):
        raise ValueError('invalid_glob')
    directory = pattern.endswith('/')
    values = _expand_braces(pattern[:-1] if directory else pattern)
    for value in values:
        if value.startswith('/') or re.match(r'^[A-Za-z]:', value):
            raise ValueError('invalid_glob')
        for part in value.split('/'):
            if part in ('', '.', '..'):
                raise ValueError('invalid_glob')
            tokens = _segment(part)
            if part != '**' and any(a is None and b is None for a, b in zip(tokens, tokens[1:])):
                raise ValueError('invalid_glob')
    values = [v + '/**' for v in values] if directory else values
    if sum(map(len, values)) > EXPANDED_TEXT_CAP:
        raise ValueError('glob_expansion_limit')
    return values


def path_key(value):
    """Conservative filesystem-independent comparison key."""
    return unicodedata.normalize('NFC', unicodedata.normalize('NFC', value).casefold())


def _directory_at(root, path, work):
    # Exact probes are cheap; folded traversal also finds untracked directories
    # on case-sensitive filesystems. Exhausted/unreadable probes never narrow scope.
    if (Path(root) / path).is_dir():
        return True
    parents = [Path(root)]
    try:
        for part in path.rstrip('/').split('/'):
            following = []
            for parent in parents:
                with os.scandir(parent) as entries:
                    for entry in entries:
                        _charge(work, 1)
                        if path_key(entry.name) == path_key(part) and entry.is_dir():
                            following.append(Path(entry.path))
            parents = following
            if not parents:
                return False
        return bool(parents)
    except (OSError, ValueError):
        return True


def directory_keys(paths, root=None):
    """Directory scopes from ancestors, explicit slashes and bounded root probes."""
    directories, work = set(), [WORK_CAP]
    for path in paths:
        parts = path.rstrip('/').split('/')
        directories.update(path_key('/'.join(parts[:i])) for i in range(1, len(parts)))
        if path.endswith('/'):
            directories.add(path_key(path.rstrip('/')))
        if root is not None and not any(c in path for c in '*?[{') and valid(path):
            if _directory_at(root, path, work):
                directories.add(path_key(path.rstrip('/')))
    return tuple(sorted(directories))


def comparison_variants(pattern, directories=()):
    directories = set(directories)
    return [path_key(v) + ('/**' if path_key(v) in directories and
                          not any(c in v for c in '*?[') else '')
            for v in expand(pattern)]


def valid(pattern):
    try:
        expand(pattern)
        return True
    except (ValueError, RecursionError):
        return False


@lru_cache(maxsize=4096)
def _segments_overlap(left, right):
    a, b = _segment(left), _segment(right)
    pending, seen = [(0, 0, False)], set()
    while pending:
        i, j, consumed = pending.pop()
        if (i, j, consumed) in seen:
            continue
        seen.add((i, j, consumed))
        if i == len(a) and j == len(b):
            if consumed:
                return True
            continue
        if i < len(a) and a[i] is None:
            pending.append((i + 1, j, consumed))
        if j < len(b) and b[j] is None:
            pending.append((i, j + 1, consumed))
        if i < len(a) and j < len(b):
            aa = ((1, 0x10ffff),) if a[i] is None else a[i]
            bb = ((1, 0x10ffff),) if b[j] is None else b[j]
            # Segments cannot consume a slash.
            if any(max(lo, low) <= min(hi, high) and
                   (max(lo, low) != 47 or min(hi, high) != 47)
                   for lo, hi in aa for low, high in bb):
                pending.append((i if a[i] is None else i + 1,
                                j if b[j] is None else j + 1, True))
    return False


@lru_cache(maxsize=4096)
def overlaps(left, right, directories=()):
    """Intersection of anchored segment globs, including future (untracked) paths."""
    work = [WORK_CAP]
    directories = tuple(set(directories) | set(directory_keys(expand(left) + expand(right))))
    for lvalue in comparison_variants(left, directories):
        for rvalue in comparison_variants(right, directories):
            _charge(work, (len(lvalue) + 1) * (len(rvalue) + 1))
            a, b = lvalue.split('/'), rvalue.split('/')
            pending, seen = [(0, 0)], set()
            while pending:
                i, j = pending.pop()
                if (i, j) in seen:
                    continue
                seen.add((i, j))
                if i == len(a) and j == len(b):
                    return True
                if i < len(a) and a[i] == '**':
                    pending.append((i + 1, j))
                if j < len(b) and b[j] == '**':
                    pending.append((i, j + 1))
                if i < len(a) and j < len(b):
                    if a[i] == '**' or b[j] == '**' or _segments_overlap(a[i], b[j]):
                        pending.append((i if a[i] == '**' else i + 1,
                                        j if b[j] == '**' else j + 1))
    return False


def _charge(work, cost):
    work[0] -= cost
    if work[0] < 0:
        raise ValueError('glob_work_limit')


def _segment_match(text, pattern):
    reachable = {0}
    for token in _segment(pattern):
        if token is None:
            reachable = set(range(min(reachable), len(text) + 1)) if reachable else set()
        else:
            reachable = {i + 1 for i in reachable if i < len(text)
                         and any(low <= ord(text[i]) <= high for low, high in token)}
    return len(text) in reachable


def match_variants(path, variants, work):
    path = path_key(path)
    parts = path.split('/')
    for value in variants:
        value = path_key(value)
        globs = value.split('/')
        _charge(work, (len(path) + len(parts)) * (len(value) + len(globs)))
        reachable = {0}
        for glob in globs:
            if glob == '**':
                reachable = set(range(min(reachable), len(parts) + 1)) if reachable else set()
            else:
                reachable = {i + 1 for i in reachable if i < len(parts) and _segment_match(parts[i], glob)}
        if len(parts) in reachable:
            return True
    return False


def matches(path, pattern):
    return match_variants(path, comparison_variants(pattern, directory_keys([path])), [WORK_CAP])


def covers_hotspot(path, pattern):
    try:
        return matches(path, pattern)
    except (ValueError, RecursionError):
        return False


def hotspot_coverage(pattern, tracked, work):
    """A glob covering more than half of tracked paths cannot hide hotspots."""
    try:
        variants = comparison_variants(pattern, directory_keys(tracked))
        matched = set()
        for path in tracked:
            if match_variants(path, variants, work):
                matched.add(path)
                if len(matched) * 2 > len(tracked):
                    return set()
        return matched
    except (ValueError, RecursionError):
        return set()
