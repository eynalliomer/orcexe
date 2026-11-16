"""Shared adapter request, template method and result contract."""

from orcexe.adapters.base import Adapter, RunRequest, validate_run_result
from orcexe.adapters.codex import CodexAdapter

__all__ = ["Adapter", "RunRequest", "validate_run_result", "CodexAdapter"]
