"""OS write/network isolation and CLI-owned control-state observations.

Only checkout and fresh command scratch are writable. A missing backend is
explicit evidence, never permission to accept. Child processes inherit policy.
On macOS, LaunchServices opens and Apple events are denied because an app they
start runs outside the profile (T48b). Mach lookups are an allow-list and bwrap
unshares every namespace with a private /run (T56).
"""

from contextvars import ContextVar
from functools import lru_cache, wraps
import hashlib
import os
from pathlib import Path
import shutil
import stat
import sys
import subprocess
import tempfile

from orcexe import ledger, proc
from orcexe.envfilter import child_env


_active = ContextVar('orcexe_isolation', default=None)


class StateTampered(ValueError):
    def __init__(self):
        super().__init__('state_tampered')


def backend():
    # Injection for hermetic regression tests and unsupported hosts.
    if os.environ.get('ORCEXE_NO_SANDBOX') == '1':
        return 'none'
    return _available_backend()


def _available_backend():
    return _probe()[0]


def unavailable_reason():
    """Why no OS sandbox is available on this host ('' when one is)."""
    if os.environ.get('ORCEXE_NO_SANDBOX') == '1':
        return 'ORCEXE_NO_SANDBOX=1'
    return _probe()[1]


def _bwrap_argv(bwrap, checkout, temporary):
    return [bwrap, '--die-with-parent', '--ro-bind', '/', '/',
            '--bind', str(checkout), str(checkout), '--bind', str(temporary), str(temporary),
            '--dev', '/dev', '--tmpfs', '/run',
            # T56: all namespaces (network included); /run hides the session bus
            # (/run/user/<uid>/bus) and the docker socket.
            '--unshare-all', '--chdir', str(checkout), '--']


def _bwrap_setup_failed(result):
    # bwrap reports its own setup errors (user namespace denied, e.g. by Ubuntu
    # 24.04 AppArmor, or a mount refused) as "bwrap: ..." with exit 1 before the
    # command starts. A child forging this only downgrades evidence to 'none'.
    return (result.status == 'exited' and result.returncode == 1
            and result.stderr_tail.lstrip().startswith('bwrap:'))


@lru_cache(maxsize=1)
def _probe():
    """Create a sandbox shaped like `run` uses; any refusal means 'none' (T63b)."""
    mode, command = None, None
    with tempfile.TemporaryDirectory(prefix='orcexe-probe-') as raw:
        scratch = Path(raw).resolve()
        if sys.platform == 'darwin' and os.access('/usr/bin/sandbox-exec', os.X_OK):
            mode = 'seatbelt'
            command = ['/usr/bin/sandbox-exec', '-p', profile(scratch, scratch), '/usr/bin/true']
        elif sys.platform.startswith('linux') and shutil.which('bwrap'):
            mode = 'bwrap'
            command = _bwrap_argv(shutil.which('bwrap'), scratch, scratch) + ['/usr/bin/true']
        if command is None:
            return 'none', 'no OS sandbox backend (sandbox-exec or bwrap) on this host'
        try:
            done = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE, env=child_env()[0], timeout=5)
        except (OSError, subprocess.TimeoutExpired) as error:
            return 'none', '{} probe failed: {}'.format(mode, type(error).__name__)
    if done.returncode == 0:
        return mode, ''
    detail = ' '.join(done.stderr.decode('utf-8', 'replace').split())[-200:]
    return 'none', '{} cannot create a sandbox on this host (exit {}): {}'.format(
        mode, done.returncode, detail or 'no stderr')


def _quote(path):
    value = str(Path(path).resolve())
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n').replace('\r', '\\r').replace('\t', '\\t') + '"'


def profile(checkout, temporary):
    return ('(version 1)\n(allow default)\n(deny file-write*)\n'
            '(deny network-outbound)\n'
            # LaunchServices (`open`) and Apple events start or drive processes
            # outside this profile, which would escape the write policy (T48b).
            '(deny lsopen)\n(deny appleevent-send)\n'
            # T56: no mach service is reachable (pasteboard, ps via launchd, ...)
            # except this allow-list, each needed by a measured tool.
            '(deny mach-lookup)\n(allow mach-lookup\n'
            # getpwuid / getgrgid (git, python, ssh resolve the user through these).
            ' (global-name "com.apple.system.opendirectoryd.libinfo")\n'
            ' (global-name "com.apple.system.opendirectoryd.membership"))\n'
            '(allow file-write* (subpath ' + _quote(checkout)
            + ') (subpath ' + _quote(temporary) + ')\n'
            ' (literal "/dev/null") (literal "/dev/tty")'
            ' (literal "/dev/dtracehelper") (subpath "/dev/fd"))\n')


def snapshot(repo):
    """Hash every control entry, including plan pointers, receipts and ledger.

    Never follow control symlinks; generation contents are walked separately.
    The CLI writes logs/receipts only after each process has returned.
    """
    root = Path(repo) / '.orcexe'
    result = {}
    if root.is_symlink():
        raise StateTampered()
    for folder, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(folder) / name
            info = path.lstat()
            kind = stat.S_IFMT(info.st_mode)
            if stat.S_ISLNK(info.st_mode):
                data = os.fsencode(os.readlink(path))
            elif stat.S_ISREG(info.st_mode):
                fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as handle:
                    data = handle.read()
            elif stat.S_ISDIR(info.st_mode):
                data = b''
            else:
                raise StateTampered()
            result[path.relative_to(root).as_posix()] = (kind, stat.S_IMODE(info.st_mode),
                                                        hashlib.sha256(data).hexdigest())
    return result


def verify(repo):
    if os.path.lexists(Path(repo) / '.orcexe/state-tampered'):
        raise StateTampered()
    try:
        ledger.Ledger(ledger.default_path(repo)).verify()
    except (OSError, ValueError, ledger.LedgerError):
        raise StateTampered() from None


class Session:
    def __init__(self, repo):
        self.repo = Path(repo)
        self.used = []
        self.tampered = False

    @property
    def mode(self):
        return 'none' if not self.used or 'none' in self.used else self.used[0]

    def poison(self):
        self.tampered = True
        # T55: the marker carries the last ledger state this command trusted, for repair.
        ledger.write_marker(self.repo / '.orcexe', ledger.trusted(ledger.default_path(self.repo)))


def guarded(function):
    """Attach per-gate isolation evidence without changing gate call signatures."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        session = Session(kwargs['repo_path'])
        token = _active.set(session)
        try:
            return function(*args, **kwargs)
        except Exception:
            if session.tampered:
                raise StateTampered() from None
            raise
        finally:
            _active.reset(token)
    return wrapped


def annotate(result):
    """Add isolation evidence before the owning gate validates/writes its receipt."""
    session = _active.get()
    if result['schema'] == 'orcexe.gate.v1':
        result['warnings'].append('isolation:' + session.mode)
    else:
        result['isolation'] = session.mode
    if session.tampered:
        if result['schema'] == 'orcexe.gate.v1':
            entry = next(c for c in result['checks'] if c['id'] == 'project_checks')
            entry.update(status='fail', detail='state_tampered')
            result['verdict'] = 'needs_fix'
        else:
            result['checks'].append(dict(id='state_tampered', status='fail', detail='state_tampered'))
            if 'verdict' in result:
                result['verdict'] = 'failed'
            if 'passed' in result:
                result['passed'] = False


def run(argv, *, cwd, env, **kwargs):
    session = _active.get()
    mode = backend()
    if session is not None:
        session.used.append(mode)
        try:
            verify(session.repo)
            before = snapshot(session.repo)
        except Exception:
            session.poison()
            raise StateTampered() from None
    with tempfile.TemporaryDirectory(prefix='orcexe-step-') as raw:
        temporary = Path(raw).resolve()
        environment = dict(env)
        for key, name in (('HOME', 'home'), ('USERPROFILE', 'home'),
                          ('XDG_CACHE_HOME', 'cache'), ('XDG_CONFIG_HOME', 'config'),
                          ('XDG_DATA_HOME', 'data'), ('CODEX_HOME', 'codex')):
            folder = temporary / name
            folder.mkdir(exist_ok=True)
            environment[key] = str(folder)
        environment.update(TMPDIR=str(temporary), TEMP=str(temporary), TMP=str(temporary))
        environment.pop('CLAUDE_CONFIG_DIR', None)
        checkout = Path(cwd).resolve()
        command = list(argv)
        if mode == 'seatbelt':
            command = ['/usr/bin/sandbox-exec', '-p', profile(checkout, temporary)] + command
        elif mode == 'bwrap':
            command = _bwrap_argv(shutil.which('bwrap'), checkout, temporary) + command
        try:
            result = proc.run(command, cwd=str(checkout), env=environment, **kwargs)
            if session is not None and mode == 'bwrap' and _bwrap_setup_failed(result):
                # The command never ran sandboxed: record unavailable, never bwrap.
                session.used.append('none')
            return result
        finally:
            if session is not None:
                try:
                    same = snapshot(session.repo) == before
                    verify(session.repo)
                except Exception:
                    same = False
                if not same:
                    session.poison()
                    raise StateTampered()


def evidence(result):
    return result.get('isolation', next((w.split(':', 1)[1] for w in result.get('warnings', [])
                                         if w.startswith('isolation:')), 'none'))


def tampered(result):
    return any(c.get('detail') == 'state_tampered' for c in result.get('checks', []))
