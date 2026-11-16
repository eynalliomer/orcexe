"""Isolated, append-only executor worktrees and verified recovery archives.

For main worktree /parent/project, worktrees live at
/parent/project-wt/<plan>/<task>[-<cand>]-<tool>, on branches
orcexe/<plan>/<task>[-<cand>]-<tool>. Paths are absolute and resolved.
Creation receipts in $GIT_COMMON_DIR/info/orcexe-worktrees preserve the spec,
base commit and ownership, even after removal. Receipts are never recycled.

Callers should use <main>/.orcexe/archive as remove's archive_root. Each new
0700 archive contains manifest.json, worktree.patch and, if there are commits
after the base, commits.bundle. Ignored files are excluded and counted.
Archiving stages the final worktree state (including untracked files) in its
index; a failed archive leaves both that index and the archive for inspection.
The main worktree's index and tracked files are never modified.

There are no concurrency/lease guarantees: callers must stop the executor
before archiving and serialize lifecycle operations.
"""

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import tempfile
from typing import Optional

from orcexe import fsio, gitutil, proc


class WorktreeError(RuntimeError):
    """An unsafe operation or incomplete archive was refused."""


@dataclass(frozen=True)
class Spec:
    plan: str
    task: str
    tool: str
    cand: Optional[str] = None

    def __post_init__(self):
        for name in ("plan", "task", "tool", "cand"):
            value = getattr(self, name)
            if name == "cand" and value is None:
                continue
            if (not isinstance(value, str) or
                    re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", value) is None or
                    ".." in value):
                raise ValueError("Invalid worktree id: {!r}".format(value))


@dataclass(frozen=True)
class Worktree:
    path: str
    branch: str
    base_commit: str
    spec: Spec
    missing: bool = False


def _git(path, *args, nul=False, allow_failure=False, index=None):
    # Match gitutil's explicit environment; never inherit HOME, credentials,
    # GIT_DIR/GIT_WORK_TREE, config injection, aliases or pagers.
    env = {key: os.environ[key] for key in
           ("PATH", "SystemRoot", "SYSTEMROOT", "WINDIR", "PATHEXT")
           if key in os.environ}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_TERMINAL_PROMPT="0",
               GIT_OPTIONAL_LOCKS="0", LC_ALL="C", LANG="C")
    if index is not None:
        env["GIT_INDEX_FILE"] = str(index)
    lines = []
    result = proc.run(["git", "--no-pager", *map(str, args)], cwd=str(path),
                      env=env, timeout_s=10, stall_s=10, on_line=lines.append)
    if result.status != "exited" or result.truncated_lines or result.pipes_held:
        raise WorktreeError("Incomplete git {}: {}".format(args[0], result.status))
    if result.returncode and not allow_failure:
        raise WorktreeError("git {} failed (exit {}): {}".format(
            args[0], result.returncode, result.stderr_tail.strip()))
    output = "\n".join(lines)
    if nul and output and not output.endswith("\0"):
        raise WorktreeError("Incomplete NUL-delimited git output")
    return result, output


def _main(repo_path):
    try:
        return Path(gitutil.main_worktree(repo_path))
    except gitutil.GitError as error:
        raise WorktreeError(str(error)) from error


def _inside(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _layout(main, spec):
    name = "-".join(filter(None, (spec.task, spec.cand, spec.tool)))
    root = main.parent / (main.name + "-wt")
    return root, root / spec.plan / name, "orcexe/{}/{}".format(spec.plan, name)


def _common(main):
    _, value = _git(main, "rev-parse", "--git-common-dir")
    return (main / value).resolve()


def _receipt(common, path):
    key = hashlib.sha256(os.fsencode(str(path))).hexdigest()
    return common / "info" / "orcexe-worktrees" / (key + ".json")


def _entries(repo_path):
    _, output = _git(repo_path, "worktree", "list", "--porcelain", "-z", nul=True)
    entries, entry = [], {}
    for field in output.split("\0"):
        if not field:
            if entry:
                entries.append(entry)
                entry = {}
            continue
        key, _, value = field.partition(" ")
        entry[key] = value
    if not entries or "worktree" not in entries[0]:
        raise WorktreeError("Missing main worktree")
    return entries


def create(repo_path, spec, base="HEAD") -> Worktree:
    """Create an owned worktree; existing branches, paths and receipts refuse reuse."""
    main = _main(repo_path)
    result, commit = _git(main, "rev-parse", "--verify", "--end-of-options",
                          str(base) + "^{commit}", allow_failure=True)
    if result.returncode:
        raise WorktreeError("base_commit_unresolved (possibly unborn): {}".format(base))
    root, target, branch = _layout(main, spec)
    result, _ = _git(main, "show-ref", "--verify", "--quiet", "refs/heads/" + branch,
                     allow_failure=True)
    if result.returncode == 0:
        raise WorktreeError("branch_exists: " + branch)
    if result.returncode != 1:
        raise WorktreeError("Cannot inspect branch: " + branch)
    if os.path.lexists(target):
        raise WorktreeError("path_exists: " + str(target))
    # Reject symlinked layout parents rather than escaping the dedicated root.
    if target.resolve() != target or root.resolve() != root:
        raise WorktreeError("worktree_layout_symlink: " + str(target))
    common = _common(main)
    receipt = _receipt(common, target)
    if receipt.exists():
        raise WorktreeError("attempt_exists: " + str(target))
    exclude = common / "info" / "exclude"
    try:
        with exclude.open(encoding="utf-8", newline="") as handle:
            previous = handle.read()
    except FileNotFoundError:
        previous = ""
    if "/.orcexe/" not in [line.removesuffix("\r") for line in previous.split("\n")]:
        fsio.atomic_write_text(exclude, previous + ("\n" if previous and
                               not previous.endswith("\n") else "") + "/.orcexe/\n")
    root.mkdir(mode=0o755, exist_ok=True)
    target.parent.mkdir(mode=0o755, exist_ok=True)
    _git(main, "worktree", "add", "-b", branch, target, commit)
    wt = Worktree(str(target.resolve()), branch, commit, spec)
    try:
        fsio.atomic_write_json(receipt, asdict(wt), mode=0o600)
    except OSError as error:
        raise WorktreeError("receipt_write_failed; worktree and branch remain: {} {}".
                            format(target, branch)) from error
    return wt


def _managed(repo_path):
    entries = _entries(repo_path)
    main = Path(entries[0]["worktree"]).resolve()
    common = _common(main)
    root = main.parent / (main.name + "-wt")
    managed = []
    for entry in entries[1:]:
        path = Path(entry["worktree"]).resolve()
        branch = entry.get("branch", "").removeprefix("refs/heads/")
        if not _inside(path, root) or not branch.startswith("orcexe/"):
            continue
        try:
            record = fsio.read_json(_receipt(common, path))
            spec = Spec(**record["spec"])
            _, expected_path, expected_branch = _layout(main, spec)
            if (record["path"] != str(path) or record["branch"] != branch or
                    expected_path != path or expected_branch != branch):
                continue
            managed.append(Worktree(str(path), branch, record["base_commit"], spec,
                                    "prunable" in entry or not path.is_dir()))
        except (OSError, ValueError, KeyError, TypeError):
            # Without a valid creation receipt, ownership cannot be established.
            continue
    return main, managed


def list_managed(repo_path) -> list[Worktree]:
    """List owned linked worktrees, retaining registered missing/prunable entries."""
    return _managed(repo_path)[1]


def is_managed(repo_path, path):
    """True only for a registered linked worktree with our layout and receipt."""
    path = str(Path(path).resolve())
    return any(wt.path == path for wt in list_managed(repo_path))


def _require_managed(repo_path, wt):
    main, managed = _managed(repo_path)
    for owned in managed:
        if (owned.path == str(Path(wt.path).resolve()) and owned.branch == wt.branch
                and owned.spec == wt.spec and owned.base_commit == wt.base_commit):
            return owned, main
    raise WorktreeError("unmanaged_worktree: " + str(wt.path))


def assert_writable(path):
    """Return {worktree, relative_path} for a linked checkout, else refuse.

    Both values are strings; relative_path is POSIX and '.' denotes the root.
    Missing children are resolved through their nearest existing directory.
    """
    path = Path(path).resolve()
    anchor = path
    while not anchor.is_dir() and anchor != anchor.parent:
        anchor = anchor.parent
    entries = _entries(anchor)
    main = Path(entries[0]["worktree"]).resolve()
    for entry in entries[1:]:
        root = Path(entry["worktree"]).resolve()
        if _inside(path, root):
            return {"worktree": str(root), "relative_path": path.relative_to(root).as_posix()}
    if _inside(path, main):
        raise WorktreeError("main_worktree_write_refused")
    raise WorktreeError("not_a_linked_worktree: " + str(path))


def status(wt) -> dict:
    """Observe HEAD, tracked/untracked changes and commits since the recorded base."""
    _, head = _git(wt.path, "rev-parse", "--verify", "HEAD^{commit}")
    _, ahead = _git(wt.path, "rev-list", "--count", wt.base_commit + ".." + head)
    try:
        dirty = gitutil.dirty(wt.path)
    except gitutil.GitError as error:
        raise WorktreeError(str(error)) from error
    return {"head": head, "dirty": dirty, "commits_ahead": int(ahead),
            "base_commit": wt.base_commit}


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(65536), b""):
            digest.update(block)
    return digest.hexdigest()


def _verify(main, dest, manifest):
    if manifest["bundle_created"]:
        _git(main, "bundle", "verify", dest / "commits.bundle")
    # A temporary index provides the HEAD tree without touching any checkout.
    # No temporary worktree is created or removed.
    with tempfile.TemporaryDirectory(prefix="orcexe-archive-index-") as scratch:
        index = Path(scratch) / "index"
        _git(main, "read-tree", manifest["head"], index=index)
        patch = dest / "worktree.patch"
        if patch.stat().st_size:
            _git(main, "apply", "--cached", "--check", "--binary", patch, index=index)
    recorded = fsio.read_json(dest / "manifest.json")
    if recorded != manifest:
        raise WorktreeError("manifest changed on disk")
    for name, checksum in recorded["sha256"].items():
        if _sha256(dest / name) != checksum:
            raise WorktreeError("sha256 mismatch: " + name)


def archive(repo_path, wt, dest_dir) -> dict:
    """Stage and archive final state; return a manifest only after verification."""
    owned, main = _require_managed(repo_path, wt)
    if os.path.lexists(dest_dir):
        raise WorktreeError("archive_exists: " + str(dest_dir))
    dest = Path(dest_dir).resolve()
    if _inside(dest, Path(owned.path)):
        raise WorktreeError("unsafe_archive_destination: " + str(dest))
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        dest.mkdir(mode=0o700)
    except FileExistsError as error:
        raise WorktreeError("archive_exists: " + str(dest)) from error
    try:
        state = status(owned)
        _, branch_head = _git(main, "rev-parse", "--verify", "refs/heads/" + wt.branch)
        if branch_head != state["head"]:
            raise WorktreeError("branch does not match worktree HEAD")
        _, ignored = _git(wt.path, "ls-files", "--others", "--ignored",
                          "--exclude-standard", "-z", nul=True)
        bundle_created = state["commits_ahead"] > 0
        files = ["worktree.patch"]
        if bundle_created:
            _git(main, "bundle", "create", dest / "commits.bundle",
                 "refs/heads/" + wt.branch, "^" + wt.base_commit)
            files.append("commits.bundle")
        _git(wt.path, "add", "-A", "--", ":/")
        _git(wt.path, "diff", "--cached", "--binary", "--full-index",
             "--no-ext-diff", "--no-textconv", "--no-renames", "--no-relative",
             "--output=" + str(dest / "worktree.patch"), "HEAD", "--")
        manifest = {"spec": asdict(wt.spec), "branch": wt.branch,
                    "base_commit": wt.base_commit, "head": state["head"],
                    "bundle_created": bundle_created,
                    "counts": {"commits_ahead": state["commits_ahead"],
                               "dirty": len(state["dirty"]),
                               "ignored": len(ignored.split("\0")[:-1])},
                    "sha256": {name: _sha256(dest / name) for name in files}}
        fsio.atomic_write_json(dest / "manifest.json", manifest, mode=0o600)
        _verify(main, dest, manifest)
    except (WorktreeError, OSError, ValueError) as error:
        raise WorktreeError("archive_unverified:{}; archive remains at {}".
                            format(error, dest)) from error
    return dict(manifest, archived_to=str(dest), verified=True)


def remove(repo_path, wt, *, archive_root) -> dict:
    """Archive, verify, then remove only this owned worktree and its branch.

    archive_root convention: <main worktree>/.orcexe/archive/.
    """
    _, main = _require_managed(repo_path, wt)
    name = "-".join(filter(None, (wt.spec.task, wt.spec.cand, wt.spec.tool)))
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    dest = Path(archive_root) / wt.spec.plan / (name + "-" + timestamp)
    result = archive(main, wt, dest)
    try:
        _require_managed(main, wt)
        _git(main, "worktree", "remove", "--force", wt.path)
    except WorktreeError as error:
        raise WorktreeError("worktree_remove_failed; archive at {}; worktree may remain at {}; "
                            "branch remains {}: {}".format(dest, wt.path, wt.branch, error)) from error
    try:
        _git(main, "branch", "-D", wt.branch)
    except WorktreeError as error:
        raise WorktreeError("branch_delete_failed; archive at {}; worktree removed; branch "
                            "remains {}: {}".format(dest, wt.branch, error)) from error
    return dict(result, removed=True)
