"""Build minimal executor environments without inheriting shell secrets."""

import os
import re


_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "LC_CTYPE",
    "TERM", "TMPDIR", "TZ", "CODEX_HOME", "CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME",
    "XDG_CACHE_HOME", "XDG_DATA_HOME", "SystemRoot", "SYSTEMROOT", "APPDATA",
    "LOCALAPPDATA", "USERPROFILE", "ComSpec", "PATHEXT", "WINDIR", "TEMP", "TMP",
})
_DENY = re.compile(
    r"(?:^|_)(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|PAT|CREDENTIALS?)(?:_|$)|"
    r"(?:^|_)KEY$|^(?:PGPASSWORD|DATABASE_URL)$|^AWS_|"
    r"^(?:OPENAI|ANTHROPIC|CURSOR|GITHUB|GH)_.*KEY",
    re.IGNORECASE,
)


def child_env(base=None, allow_extra=()):
    """Return (fresh environment, sorted names excluded by the deny rule)."""
    if isinstance(allow_extra, str):
        raise TypeError("allow_extra must be an iterable of names, not str")
    if base is None:
        base = os.environ
    allowed = _ALLOWLIST.union(allow_extra)
    if os.name == "nt":
        allowed = {name.upper() for name in allowed}
    env = {}
    dropped = []
    for name, value in base.items():
        if _DENY.search(name):
            dropped.append(name)
        elif (name.upper() if os.name == "nt" else name) in allowed:
            env[name] = value
    return env, sorted(dropped)
