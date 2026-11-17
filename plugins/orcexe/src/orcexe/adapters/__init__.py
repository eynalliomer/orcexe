"""Shared adapter request, template method and result contract."""

from orcexe.adapters.base import Adapter, RunRequest, validate_run_result
from orcexe.adapters.codex import CodexAdapter
from orcexe.adapters.claude_code import ClaudeCodeAdapter

__all__ = ["Adapter", "RunRequest", "validate_run_result", "CodexAdapter", "ClaudeCodeAdapter"]
