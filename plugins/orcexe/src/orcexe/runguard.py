"""CLI-state observation around every external model run (P2-2, T55).

The CLI never writes the ledger or any run directory while an executor, L1
evaluator or (later) auditor runs. ``Guard.run`` therefore observes, before and
after each external call, the ledger bytes (size + sha256), the head ref and a
fingerprint of the whole ``.orcexe/runs`` tree (every dispatch). Any ledger or ref
change, and any run-directory change other than the adapter's fixed artifacts in
that call's own output directory, is foreign: the guard writes the ``state-tampered``
marker carrying the last trusted ledger state and raises ``isolation.StateTampered``.
With ``with_main`` it also compares the main-repository snapshot the executor window
uses (config, plan, decisions, refs, status): a change is ``main_repository_changed``.
Flags are sticky, so a caller whose code swallows exceptions (L1 evaluation) still
sees them through ``check``. Decisions never re-read an adapter artifact from disk:
the L1 verdict comes from ``adapter.final_text`` captured when the run ended.
"""

import hashlib
import os
from pathlib import Path
import stat

from orcexe import isolation, ledger


# Fixed adapter artifacts relative to the call's own run_dir (base, Codex, Claude, Cursor).
ADAPTER_OUTPUTS = frozenset(('.attempt', 'brief.md', 'brief', 'brief/brief.md', 'final.md',
                             'events.jsonl', 'stderr.txt', 'result.json'))


def _ledger_state(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))
    except FileNotFoundError:
        return None
    digest, size = hashlib.sha256(), 0
    with os.fdopen(fd, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
            size += len(chunk)
    return size, digest.hexdigest()


def fingerprint(root):
    """``{relative: (kind, mode, sha256)}`` without following links; directory mtimes are ignored."""
    root = Path(root)
    result = {}
    if not os.path.lexists(root):
        return result
    if root.is_symlink() or not root.is_dir():
        return {'.': ('other', 0, '')}
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            info = path.lstat()
            mode = stat.S_IMODE(info.st_mode)
            if stat.S_ISDIR(info.st_mode):
                entry = ('dir', mode, '')
            elif stat.S_ISLNK(info.st_mode):
                entry = ('link', mode, hashlib.sha256(os.fsencode(os.readlink(path))).hexdigest())
            elif stat.S_ISREG(info.st_mode):
                digest = hashlib.sha256()
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
                with os.fdopen(fd, 'rb') as handle:
                    for chunk in iter(lambda: handle.read(1 << 20), b''):
                        digest.update(chunk)
                entry = ('file', mode, digest.hexdigest())
            else:
                entry = ('other', mode, '')
            result[path.relative_to(root).as_posix()] = entry
    return result


MARKER = '.orcexe/' + ledger.MARKER


def check_marker(main):
    """An earlier terminal tamper is reported before any ledger verdict."""
    if os.path.lexists(Path(main) / MARKER):
        raise isolation.StateTampered()


class Guard:
    """One observer for every external run (executor, L1 evaluator, later auditors).

    ``run_root`` is the main ``.orcexe/runs`` directory, so other dispatches' run
    directories are covered too. ``main_snapshot`` (optional) is the same main-repository
    observation the executor window is compared with (config, plan, decisions, refs,
    status); a change there during a guarded call is ``main_repository_changed``.
    """

    def __init__(self, main, run_root, log, main_snapshot=None):
        self.main = Path(main)
        self.run_root = Path(run_root)
        self.log = log
        self.main_snapshot = main_snapshot
        self.tampered = False
        self.main_changed = False

    def observe(self, with_main=False):
        anchor = self.log._anchor()
        head = anchor.read(strict=False) if anchor is not None else None
        main = self.main_snapshot() if with_main and self.main_snapshot is not None else None
        return _ledger_state(self.log.path), head, fingerprint(self.run_root), main

    def _allowed(self, before, after, own):
        """Only the adapter's fixed artifacts in the call's own directory may change."""
        try:
            prefix = Path(own).resolve().relative_to(self.run_root.resolve()).as_posix()
        except ValueError:
            return before == after
        prefix = '' if prefix == '.' else prefix + '/'
        ancestors = {''}
        parts = prefix.rstrip('/').split('/') if prefix else []
        for index in range(1, len(parts) + 1):
            ancestors.add('/'.join(parts[:index]))
        for key in set(before) | set(after):
            if before.get(key) == after.get(key):
                continue
            if key in before and key not in after:
                return False  # The CLI's evidence never disappears during a call.
            if key in ancestors and after[key][0] == 'dir':
                continue  # The adapter may create its own (fresh) output directory.
            relative = key[len(prefix):] if key.startswith(prefix) else None
            if relative not in ADAPTER_OUTPUTS:
                return False
            if after[key][0] != ('dir' if relative == 'brief' else 'file'):
                return False
        return True

    def run(self, call, own, with_main=False):
        """Return ``call()``; raise StateTampered (and stay flagged) on any foreign change."""
        try:
            before = self.observe(with_main)
        except Exception:
            self.flag()
            raise isolation.StateTampered() from None
        try:
            return call()
        finally:
            try:
                after = self.observe(with_main)
                clean = (before[0] == after[0] and before[1] == after[1]
                         and self._allowed(before[2], after[2], own))
                same_main = before[3] == after[3]
            except Exception:
                clean = same_main = False
            if not clean:
                self.flag()
                raise isolation.StateTampered()
            if not same_main:
                self.main_changed = True
                raise ValueError('main_repository_changed')

    def flag(self):
        self.tampered = True
        # The marker carries the last ledger state this command trusted, for repair.
        ledger.write_marker(self.main / '.orcexe', ledger.trusted(self.log.path))

    def check(self):
        if self.tampered:
            raise isolation.StateTampered()
        if self.main_changed:
            raise ValueError('main_repository_changed')

    def wrap(self, adapter, with_main=False):
        return _Guarded(self, adapter, with_main)


class _Guarded:
    """An adapter whose every run is guarded; everything else is delegated."""

    def __init__(self, guard, adapter, with_main):
        self._guard, self._adapter, self._with_main = guard, adapter, with_main

    def run(self, req):
        return self._guard.run(lambda: self._adapter.run(req), req.run_dir, self._with_main)

    def __getattr__(self, name):
        return getattr(self._adapter, name)
