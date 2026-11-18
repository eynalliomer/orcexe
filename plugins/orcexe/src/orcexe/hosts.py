"""Presence-only host detection; declarations are not runtime verification."""

import os
from pathlib import Path

from orcexe.miniyaml import load_path

PLUGIN = Path(__file__).resolve().parents[2]
HOST_FILE = PLUGIN / "hosts" / "claude-code.yaml"
SKILL_PATH = PLUGIN / "skills" / "orcexe" / "SKILL.md"
_CODES = tuple("H" + str(n) for n in range(1, 8))


def detect_host(*, env=None, subprocess_ok=False, skill_path=None):
    """Read environment names only. Only H1/H2 can be measured in v0.1."""
    env = os.environ if env is None else env
    marker = next((name for name in ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CLAUDECODE")
                   if name in env), None)
    codex = marker in ("CODEX_THREAD_ID", "CODEX_SANDBOX")
    tool = "codex" if codex else "claude-code"
    declaration = load_path(PLUGIN / "hosts" / "codex.yaml" if codex else HOST_FILE)
    if (set(declaration) != {"host", "detect", "capabilities", "meanings"} or
            declaration["host"] != tool or
            declaration["detect"] != {"env_any": ["CODEX_THREAD_ID", "CODEX_SANDBOX"] if codex else ["CLAUDECODE"]} or
            set(declaration["capabilities"]) != set(_CODES) or
            set(declaration["meanings"]) != set(_CODES) or
            any(v not in (("info",) if codex else ("required", "preferred", "optional"))
                for v in declaration["capabilities"].values()) or
            any(not isinstance(v, str) or not v for v in declaration["meanings"].values())):
        raise ValueError("Invalid host declaration")
    found = marker is not None
    capabilities = []
    for code in _CODES if found else _CODES[:2]:
        measured = found and not codex and ((code == "H1" and subprocess_ok) or
                             (code == "H2" and Path(skill_path or SKILL_PATH).is_file()))
        message = declaration["meanings"][code]
        if not found:
            message += ": no host detected"
        elif measured:
            message += ": verified" if code == "H1" else ": skill file exists"
        else:
            message += ": " + declaration["capabilities"][code] + " (declared)"
        capabilities.append(dict(code=code, status="pass" if measured else "info",
                                 evidence="measured" if measured else "declared",
                                 message=message, fix=None))
    return dict(tool=tool if found else "none",
                detected_by="env:" + marker if found else "none",
                capabilities=capabilities)
