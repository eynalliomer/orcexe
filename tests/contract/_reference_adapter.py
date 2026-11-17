"""A fixture-only adapter; it never locates or invokes the real Codex CLI."""

from pathlib import Path
import hashlib
import json
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))

from orcexe.adapters import Adapter, CodexAdapter, ClaudeCodeAdapter
from tests.unit._timing import scaled


class ReferenceAdapter(Adapter):
    def auth_status(self):
        return {"source": "fixture", "authenticated": None}

    def list_models(self):
        return []

    def quota(self):
        return {"windows": [], "source": "none"}

    def _classify(self, events_summary, proc_result):
        counts = events_summary["counts"]
        completed = counts.get("turn.completed", 0)
        failed = counts.get("turn.failed", 0)
        status = "completed" if completed and not failed and proc_result.returncode == 0 else "failed"
        # Deliberately no provider-specific interpretation of auth/quota strings.
        return status, [] if completed or failed else ["final_event_missing"]

    def _extract(self, events):
        text, usage = "", {}
        for event in events:
            if not isinstance(event, dict):
                continue
            item = event.get("item", {})
            if (event.get("type") == "item.completed" and isinstance(item, dict) and
                    item.get("type") == "agent_message"):
                text = item.get("text", "")
            if event.get("type") == "turn.completed":
                raw = event.get("usage", {})
                usage = {key: raw.get(source) for key, source in {
                    "input": "input_tokens", "cached_read": "cached_input_tokens",
                    "cache_write": "cache_write_input_tokens", "output": "output_tokens",
                    "reasoning": "reasoning_output_tokens"}.items()}
        return {"final_text": text, "usage": usage, "model_actual": None}


# T13 registers the production Codex adapter here; the same inherited test class
# is instantiated for every registry entry by test_adapter_contract.
ADAPTERS = {"reference": ReferenceAdapter, "codex": CodexAdapter, "claude-code": ClaudeCodeAdapter}
ADAPTER_OPTIONS = {"codex": {"appserver_timeout_s": scaled(5)}}


def expected_outcome(adapter, scenario, request):
    """Provider semantics vary; evidence and lifecycle assertions stay shared."""
    status = {"invalid-model": "failed", "auth-401": "failed", "quota": "failed",
              "quota-unconfirmed": "failed", "no-final": "failed", "stall": "stalled"}.get(scenario, "completed")
    warnings = {"no-final": ["final_event_missing"], "commit": ["head_changed"],
                "empty-write": ["empty_diff"]}.get(scenario, [])
    if scenario == "background-child" and sys.platform != "win32":
        warnings = ["pipes_held"]
    if adapter == "claude-code":
        status = {"quota": "quota", "auth-missing": "auth"}.get(scenario, status)
        warnings += {"invalid-model": ["model_not_found"], "model-mismatch": ["model_mismatch"]}.get(scenario, [])
    if adapter == "codex":
        status = {"auth-401": "auth", "quota": "quota"}.get(scenario, status)
        warnings = warnings + {"invalid-model": ["model_not_found"],
                               "quota-unconfirmed": ["quota_suspected"]}.get(scenario, [])
        if scenario in ("write", "commit", "empty-write"):
            header = "[projects.{}]".format(json.dumps(str(Path(request.workdir).resolve()), ensure_ascii=False))
            warnings = warnings + ["trust_entry_added:" + hashlib.sha256(header.encode()).hexdigest()[:12]]
    return status, warnings
