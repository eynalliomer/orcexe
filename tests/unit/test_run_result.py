"""The result file is a checked boundary, including nested schema mutations."""

from copy import deepcopy
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "plugins" / "orcexe" / "src"))
from orcexe.adapters import RunRequest, validate_run_result


def valid_result(directory):
    directory = Path(directory).resolve()
    return {"schema": "orcexe.run.v1", "run_id": "T1-r1", "task": "T1", "tool": "codex",
        "tool_version": "0.163.0", "family": "openai", "model_requested": "gpt-6-luna",
        "model_actual": None, "effort": "low", "profile": "review", "context": "fresh",
        "status": "completed", "exit_code": 0, "signal": None,
        "final_text_path": str(directory / "final.md"),
        "events_path": str(directory / "events.jsonl"),
        "stderr_tail_path": str(directory / "stderr.txt"),
        "head_before": "a" * 40, "head_after": "a" * 40,
        "dirty_before": [], "diff_files": [], "untracked": [],
        "self_report": {"exit": None, "needs": []},
        "usage": dict.fromkeys(("input", "cached_read", "cache_write", "output", "reasoning"), 0),
        "cost": {"list_usd": None, "basis": "unknown"},
        "quota_after": {"windows": [], "source": "none"}, "duration_ms": 0, "warnings": []}


class RunResultTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="orcexe-result-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.valid = valid_result(self.directory)

    def test_valid_result_and_every_required_key(self):
        validate_run_result(self.valid, run_dir=self.directory)
        for key in self.valid:
            with self.subTest(missing=key):
                result = deepcopy(self.valid)
                del result[key]
                with self.assertRaises(ValueError):
                    validate_run_result(result)

    def test_every_top_level_wrong_type(self):
        for key in self.valid:
            with self.subTest(key=key):
                result = deepcopy(self.valid)
                result[key] = object()
                with self.assertRaises(ValueError):
                    validate_run_result(result)

    def test_mutation_table(self):
        mutations = [
            {"status": "DONE"}, {"exit_code": 1}, {"exit_code": True},
            {"signal": True}, {"signal": 9}, {"duration_ms": -1}, {"duration_ms": True},
            {"profile": "write"}, {"head_after": "b" * 40}, {"extra": "unknown"},
            {"head_before": "not-a-sha", "head_after": "not-a-sha"},
            {"model_actual": "another-model"}, {"context": "reuse"}, {"schema": "orcexe.run.v2"},
            {"run_id": "../escape"}, {"run_id": "x\n"},
            {"warnings": [1]}, {"dirty_before": ["../escape"]},
            {"diff_files": ["/absolute"]}, {"untracked": ["C:/outside"]},
            {"events_path": str(self.directory.parent / "events.jsonl")},
            {"final_text_path": "relative/final.md"},
            {"events_path": str(self.directory / ".." / "events.jsonl")},
            {"self_report": {"exit": "BOGUS", "needs": []}},
            {"self_report": {"exit": [], "needs": []}},
            {"self_report": {"exit": None, "needs": "context"}},
            {"self_report": {"exit": None, "needs": [], "unknown": 1}},
            {"cost": {"list_usd": -1, "basis": "tool"}},
            {"cost": {"list_usd": float("nan"), "basis": "tool"}},
            {"cost": {"list_usd": True, "basis": "tool"}},
            {"cost": {"list_usd": None, "basis": "guessed"}},
            {"cost": {"list_usd": None, "basis": "unknown", "extra": None}},
            {"quota_after": {"windows": "none", "source": "none"}},
            {"quota_after": {"windows": [], "source": "model"}},
            {"quota_after": {"windows": [], "source": "none", "extra": None}},
        ]
        for field in self.valid["usage"]:
            for value in (-1, True, 1.2, "1"):
                usage = dict(self.valid["usage"], **{field: value})
                mutations.append({"usage": usage})
        mutations += [{"usage": {}}, {"usage": dict(self.valid["usage"], extra=1)}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                result = deepcopy(self.valid)
                result.update(mutation)
                with self.assertRaises(ValueError):
                    validate_run_result(result)

    def test_warning_invariants_and_informational_self_report(self):
        result = deepcopy(self.valid)
        result.update(profile="write", model_actual="different", head_after=None,
                      warnings=["empty_diff", "head_changed", "model_mismatch"])
        validate_run_result(result)
        result.update(status="failed", exit_code=1, self_report={"exit": "DONE", "needs": []})
        validate_run_result(result)
        self.assertEqual(result["status"], "failed")
        result["usage"] = dict.fromkeys(result["usage"])
        validate_run_result(result)
        with self.assertRaises(ValueError):
            validate_run_result(result, run_dir=self.directory / "other")

    def test_run_request_validation_and_frozen_required_fields(self):
        brief = self.directory / "source.md"
        brief.write_text("brief", encoding="utf-8")
        req = RunRequest("T1-r1", "T1", "codex", "gpt-6-luna", "low", "review",
                         str(self.directory), str(brief), str(self.directory / "run"))
        self.assertEqual((req.timeout_s, req.stall_s), (1800, 300))
        with self.assertRaises(FrozenInstanceError):
            req.model = "other"
        with self.assertRaises(TypeError):
            RunRequest(run_id="T1", task="T1", tool="codex", profile="review",
                       workdir=str(self.directory), brief_path=str(brief), run_dir=str(self.directory))
        mutations = {"run_id": ("", "-bad", "a" * 65, "bad/name", "bad\n", None),
                     "profile": ("bad", None), "model": ("", None), "effort": ("", None),
                     "workdir": ("relative", str(brief)), "brief_path": ("relative", str(self.directory)),
                     "run_dir": ("relative",), "timeout_s": (0, -1, True, float("inf")),
                     "stall_s": (0, -1, float("nan"))}
        for key, values in mutations.items():
            for value in values:
                with self.subTest(key=key, value=value), self.assertRaises(ValueError):
                    replace(req, **{key: value})
