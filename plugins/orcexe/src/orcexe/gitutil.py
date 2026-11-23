"""Git observations through the bounded runner, without shell or user config.

Paths in observations are relative to the repository root. Renames include
both the old and new name. Content snapshots distinguish edits to dirty paths.
"""

import os
import hashlib
from pathlib import Path
import stat

from orcexe import proc, redact


class GitError(RuntimeError):
    """Git could not produce a complete, trustworthy observation.

    `reason` is the stable short message; `returncode` and `stderr_tail` (redacted,
    home/repository paths replaced) make CI failures diagnosable (T63b).
    """

    def __init__(self, reason, returncode=None, stderr_tail=""):
        message = reason
        if returncode is not None:
            message += " (exit {})".format(returncode)
        if stderr_tail:
            message += ": " + stderr_tail
        super().__init__(message)
        self.reason = reason
        self.returncode = returncode
        self.stderr_tail = stderr_tail


# Bytes of git stderr kept by the runner; shorter tails may cut a message's start.
_STDERR_BYTES = 4096
# Characters of the redacted tail carried by GitError.
_DIAGNOSTIC_CHARS = 240


def _diagnostic(stderr, path):
    """A short, single-line, redacted stderr tail without home or repository paths."""
    text = redact.redact_text(stderr or "")
    replacements = []
    for value, label in ((path, "<path>"), (os.environ.get("HOME"), "~")):
        if not value:
            continue
        candidates = {str(value)}
        try:
            candidates.add(str(Path(value).resolve()))
        except (OSError, RuntimeError):
            pass
        replacements.extend((candidate, label) for candidate in candidates
                            if len(candidate) > 1)
    # Longest first: the repository path usually lives below the home path.
    for value, label in sorted(replacements, key=lambda item: -len(item[0])):
        text = text.replace(value, label)
    text = " ".join(text.split())
    if len(text) > _DIAGNOSTIC_CHARS:
        text = "..." + text[-(_DIAGNOSTIC_CHARS - 3):]
    return text


def _environment():
    # No credentials, GIT_DIR/GIT_WORK_TREE overrides, global aliases or pagers.
    # LC_ALL/LANG=C (and no inherited LANGUAGE) keep git messages untranslated.
    env = {key: os.environ[key] for key in
           ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT")
           if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", LC_ALL="C", LANG="C")
    return env


def _git(path, *args, nul=False, allow_failure=False):
    lines = []
    result = proc.run(["git", "--no-pager", *args], cwd=str(path), env=_environment(),
                      timeout_s=10, stall_s=10, on_line=lines.append,
                      stderr_tail_bytes=_STDERR_BYTES)
    if (result.status != "exited" or result.truncated_lines or result.pipes_held):
        raise GitError("Incomplete git observation: " + result.status, result.returncode,
                       _diagnostic(result.stderr_tail, path))
    if result.returncode and not allow_failure:
        raise GitError("Git {} failed".format(args[0]), result.returncode,
                       _diagnostic(result.stderr_tail, path))
    # proc delivers newline-free lines, including the final NUL-terminated
    # fragment at EOF. Restore embedded newlines, not the artificial last one.
    output = "\n".join(lines)
    if nul and output and not output.endswith("\0"):
        raise GitError("Incomplete NUL-delimited git output")
    return result, output


def resolve_repo_path(path):
    """Resolve a repository path for `--repo`; missing paths are not repositories."""
    root = Path(path)
    if not root.exists():
        raise GitError("not a repository")
    try:
        return root.resolve()
    except OSError as error:
        raise GitError("not a repository") from error


# Ownership refusals are a distinct, fixable error, never "not a repository".
_UNTRUSTED = ("dubious ownership", "safe.directory")
# Discovery found no repository: plain, ceiling directories, filesystem boundary
# (GIT_DISCOVERY_ACROSS_FILESYSTEM), or a gitfile naming a missing directory.
_NOT_A_REPOSITORY = ("not a git repository", "stopping at filesystem boundary",
                     "git_discovery_across_filesystem")
# Only when the runner cut the start of stderr: the remaining discovery suffix.
_CUT_SUFFIXES = ("git repository", "(or any of the parent directories)",
                 "(or any parent up to mount point")


def _not_a_repository(returncode, stderr, path):
    """True for git's discovery failure; raise for anything else, including ownership."""
    text = " ".join((stderr or "").lower().split())
    if returncode == 128 and any(marker in text for marker in _UNTRUSTED):
        raise GitError("Repository ownership is not trusted (safe.directory)", returncode,
                       _diagnostic(stderr, path))
    if returncode == 128:
        if any(marker in text for marker in _NOT_A_REPOSITORY):
            return True
        cut = len((stderr or "").encode("utf-8")) >= _STDERR_BYTES
        if cut and any(marker in text for marker in _CUT_SUFFIXES):
            return True
    raise GitError("Cannot determine whether path is a repository", returncode,
                   _diagnostic(stderr, path))


def is_repo(path):
    result, text = _git(path, "rev-parse", "--is-inside-work-tree", allow_failure=True)
    if result.returncode:
        return not _not_a_repository(result.returncode, result.stderr_tail, path)
    return text == "true"


def toplevel(path):
    _, text = _git(path, "rev-parse", "--show-toplevel")
    return str(Path(text).resolve())


def ls_files(path) -> list[str]:
    """Return tracked, root-relative POSIX paths, preserving NUL-safe names."""
    _, text = _git(path, "ls-files", "--full-name", "-z", "--", ":/", nul=True)
    return text.split("\0")[:-1]


def main_worktree(path):
    _, text = _git(path, "worktree", "list", "--porcelain", "-z", nul=True)
    first = text.split("\0", 1)[0]
    if not first.startswith("worktree "):
        raise GitError("Missing main worktree")
    return str(Path(first[len("worktree "):]).resolve())


def head(path):
    result, text = _git(path, "rev-parse", "--verify", "HEAD", allow_failure=True)
    if result.returncode == 0:
        return text
    # Verify this is an unborn symbolic branch, rather than hiding corruption,
    # a missing executable, an inaccessible repo, or an invalid HEAD ref.
    _git(path, "symbolic-ref", "-q", "HEAD")
    result, _ = _git(path, "show-ref", "--head", allow_failure=True)
    if result.returncode not in (0, 1):
        raise GitError("Cannot inspect unborn HEAD")
    result, _ = _git(path, "rev-parse", "--verify", "--quiet", "HEAD", allow_failure=True)
    if result.returncode != 1:
        raise GitError("Cannot inspect HEAD")
    return None


def _porcelain(text):
    """Parse porcelain v1 -z (destination first, then source for renames)."""
    records = iter(text.split("\0")[:-1])
    files, untracked = set(), set()
    for record in records:
        if len(record) < 4 or record[2] != " ":
            raise GitError("Malformed porcelain record")
        status, name = record[:2], record[3:]
        files.add(name)
        if status == "??":
            untracked.add(name)
        if "R" in status or "C" in status:
            try:
                files.add(next(records))
            except StopIteration as error:
                raise GitError("Missing rename source") from error
    return sorted(files), sorted(untracked)


def _status(path):
    _, text = _git(path, "status", "--porcelain=v1", "-z",
                   "--untracked-files=all", nul=True)
    return _porcelain(text)


def dirty(path):
    return _status(path)[0]


def fingerprints(path, files):
    """Hash regular files/link targets; compare other paths by stable markers."""
    root = Path(toplevel(path))
    result = {}
    for name in files:
        target = root / name
        try:
            mode = target.lstat().st_mode
            if stat.S_ISLNK(mode):
                result[name] = hashlib.sha256(os.fsencode(os.readlink(target))).hexdigest()
            elif stat.S_ISDIR(mode):
                # Gitlinks are directories; their state is observed by Git.
                result[name] = "directory"
            elif not stat.S_ISREG(mode):
                result[name] = "non-regular"
            else:
                digest = hashlib.sha256()
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(128 * 1024), b""):
                        digest.update(chunk)
                result[name] = digest.hexdigest()
        except FileNotFoundError:
            result[name] = None
        except OSError:
            result[name] = "unreadable"
    return result


def changed_since(path, head_before, dirty_before, fingerprints_before=None):
    current_head = head(path)
    files, untracked = _status(path)
    args = ["diff", "--name-only", "--no-renames", "--no-relative", "-z"]
    # Comparing to the original HEAD also detects changes committed by an agent.
    if head_before is not None:
        _, text = _git(path, *args, head_before, "--", nul=True)
        files.extend(text.split("\0")[:-1])
    elif current_head is not None:
        _, text = _git(path, "ls-tree", "-r", "--name-only", "-z", "HEAD", nul=True)
        files.extend(text.split("\0")[:-1])
    changed = set(files) - set(dirty_before)
    if fingerprints_before is not None:
        current = fingerprints(path, dirty_before)
        changed.update(name for name in dirty_before
                       if current[name] != fingerprints_before[name])
    return {"head": current_head,
            "diff_files": sorted(changed),
            "untracked": untracked}
