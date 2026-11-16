"""UTF-8 state helpers with file-level fsync and atomic replacement.

New parent directories use dir_mode (0700 by default, subject to umask).
Existing directory modes are preserved. Directory fsync is not performed, so
file fsync alone does not guarantee directory entries survive a power loss.
"""

import json
import os
from pathlib import Path
import tempfile


_MISSING = object()


def _make_parents(directory, dir_mode):
    # Path.mkdir(parents=True) does not apply mode to intermediate parents.
    missing = []
    while not directory.exists():
        missing.append(directory)
        directory = directory.parent
    for parent in reversed(missing):
        try:
            parent.mkdir(mode=dir_mode)
        except FileExistsError:
            if not parent.is_dir():
                raise


def atomic_write_text(path, text, mode=0o644, dir_mode=0o700):
    """Replace path only after a complete, flushed write in its directory."""
    path = Path(path)
    _make_parents(path.parent, dir_mode)
    fd, temporary = tempfile.mkstemp(prefix=".{}-".format(path.name), dir=str(path.parent))
    try:
        handle = os.fdopen(fd, "w", encoding="utf-8", newline="\n")
        fd = None  # The handle now owns the descriptor.
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(temporary)
        except OSError:
            pass  # Preserve the original exception, including after replacement.
        raise


def atomic_write_json(path, obj, mode=0o644, dir_mode=0o700):
    """Write deterministic, readable JSON followed by a newline."""
    text = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n"
    atomic_write_text(path, text, mode=mode, dir_mode=dir_mode)


def read_json(path, default=_MISSING):
    """Read JSON, using default only when the file does not exist."""
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default
    except ValueError as error:
        # Do not include file contents, which may contain sensitive values.
        raise ValueError("Invalid JSON in {}".format(path)) from error


def read_jsonl(path):
    """Read newline-delimited records without splitting Unicode separators."""
    with open(path, encoding="utf-8", newline="\n") as handle:
        text = handle.read()
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    try:
        return [json.loads(line) for line in lines]
    except ValueError as error:
        raise ValueError("Invalid JSONL in {}".format(path)) from error


def append_jsonl(path, obj, dir_mode=0o700):
    """Append and fsync one record; newly created ledgers use mode 0600."""
    line = json.dumps(obj, ensure_ascii=False, sort_keys=True, allow_nan=False)
    # Also support consumers that use str.splitlines() instead of split("\n").
    for character in ("\u2028", "\u2029", "\u0085", "\x0c"):
        line = line.replace(character, "\\u{:04x}".format(ord(character)))
    if "\n" in line or "\r" in line:
        raise ValueError("JSONL serialization contains a raw newline")
    path = Path(path)
    _make_parents(path.parent, dir_mode)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        handle = os.fdopen(fd, "a", encoding="utf-8", newline="\n")
    except BaseException:
        os.close(fd)
        raise
    with handle:
        handle.write(line + "\n")
        handle.flush()
        os.fsync(handle.fileno())
