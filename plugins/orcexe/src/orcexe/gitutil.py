"""Git observations through the bounded runner, without shell or user config.

Paths in observations are relative to the repository root. Renames include
both the old and new name. A pre-existing dirty path is excluded as a whole
from changed_since; this is a path snapshot, not attribution of individual edits.
"""

import os
from pathlib import Path

from orcexe import proc


class GitError(RuntimeError):
    """Git could not produce a complete, trustworthy observation."""


def _git(path, *args, nul=False, allow_failure=False):
    # No credentials, GIT_DIR/GIT_WORK_TREE overrides, global aliases or pagers.
    env = {key: os.environ[key] for key in
           ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT")
           if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", LC_ALL="C", LANG="C")
    lines = []
    result = proc.run(["git", "--no-pager", *args], cwd=str(path), env=env,
                      timeout_s=10, stall_s=10, on_line=lines.append)
    if (result.status != "exited" or result.truncated_lines or result.pipes_held):
        raise GitError("Incomplete git observation: " + result.status)
    if result.returncode and not allow_failure:
        raise GitError("Git {} failed (exit {})".format(args[0], result.returncode))
    # proc delivers newline-free lines, including the final NUL-terminated
    # fragment at EOF. Restore embedded newlines, not the artificial last one.
    output = "\n".join(lines)
    if nul and output and not output.endswith("\0"):
        raise GitError("Incomplete NUL-delimited git output")
    return result, output


def is_repo(path):
    result, text = _git(path, "rev-parse", "--is-inside-work-tree", allow_failure=True)
    if result.returncode:
        if result.returncode == 128 and "not a git repository" in result.stderr_tail:
            return False
        raise GitError("Cannot determine whether path is a repository")
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


def changed_since(path, head_before, dirty_before):
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
    return {"head": current_head,
            "diff_files": sorted(set(files) - set(dirty_before)),
            "untracked": untracked}
