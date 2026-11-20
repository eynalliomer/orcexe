"""Stdlib-only building blocks for external fake CLI fixtures."""

import json
import os
from pathlib import Path
import sys


def edit_file(directory):
    """Default-off append hook; an empty text leaves an existing file intact."""
    name = os.environ.get("ORCEXE_FAKE_EDIT_FILE")
    text = os.environ.get("ORCEXE_FAKE_EDIT_TEXT")
    if name is None or text is None:
        return False
    relative = Path(name)
    root = Path(directory).resolve()
    target = (root / relative).resolve()
    if (not name or relative.is_absolute() or ".." in relative.parts
            or root not in target.parents):
        raise ValueError("fake edit path must be repo-relative")
    with target.open("a", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return True


def emit(value):
    print(json.dumps(value, separators=(",", ":")), flush=True)


def signal_ready():
    """Optional test handshake, default off.

    ORCEXE_FAKE_READY_FILE is touched only after initial events are flushed.
    Tests may prepare the fixture before starting the runner's timeout clock.
    """
    target = os.environ.get("ORCEXE_FAKE_READY_FILE")
    if target:
        Path(target).touch()


def record_call(argv, scenario, model_call):
    """One O_APPEND write per record, including under concurrent invocations."""
    target = os.environ.get("ORCEXE_FAKE_CALLS")
    if target:
        record = {"argv": argv, "cwd": os.getcwd(), "scenario": scenario,
                  "model_call": model_call}
        data = (json.dumps(record) + "\n").encode("utf-8")
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.write(fd, data) != len(data):
                raise OSError("short fake call-log write")
        finally:
            os.close(fd)


def in_git_repo(directory):
    directory = Path(directory).resolve()
    for parent in (directory,) + tuple(directory.parents):
        entry = parent / '.git'
        if entry.is_dir():
            return (entry / 'HEAD').is_file()
        if entry.is_file():
            text = entry.read_text(encoding='utf-8').strip()
            return (text.startswith('gitdir: ') and
                    (parent / text[len('gitdir: '):]).is_dir())
        # Git stops discovery at a bare repository. Test homes can therefore
        # stay private even when TMPDIR is inside the source checkout.
        if ((parent / 'HEAD').is_file() and (parent / 'objects').is_dir()
                and (parent / 'refs').is_dir()):
            return False
    return False


def trust_project(directory, sandbox, is_git):
    if sandbox != "workspace-write" or not is_git:
        return
    home = os.environ.get("CODEX_HOME")
    if not home:
        print("orcexe-fake: CODEX_HOME not set, trust write skipped",
              file=sys.stderr, flush=True)
        return
    config = Path(home) / "config.toml"
    # JSON quoting is also TOML basic-string quoting for filesystem paths.
    header = "[projects.{}]".format(json.dumps(str(Path(directory).resolve()),
                                               ensure_ascii=False))
    config.parent.mkdir(parents=True, exist_ok=True)
    if config.exists() and header in config.read_text(encoding="utf-8").splitlines():
        return
    with config.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write('\n{}\ntrust_level = "trusted"\n'.format(header))


def read_stream(path):
    with path.open(encoding="utf-8") as stream:
        header = json.loads(next(stream))
        if "_fake" not in header:
            raise ValueError("fake stream is missing its evidence header")
        events = [json.loads(line) for line in stream]
    replacement = os.environ.get("ORCEXE_FAKE_FINAL_TEXT_FILE")
    if replacement and path.name in ("claude-success.jsonl", "codex-success.jsonl"):
        text = Path(replacement).read_text(encoding="utf-8")
        for event in events:
            if event.get("type") == "assistant":
                event["message"]["content"] = [{"type": "text", "text": text}]
            elif event.get("type") == "result" and event.get("subtype") == "success":
                event["result"] = text
            elif event.get("item", {}).get("type") == "agent_message":
                event["item"]["text"] = text
    return events


def read_payload(directory, name):
    with (directory / (name + ".json")).open(encoding="utf-8") as stream:
        return json.load(stream)["payload"]
