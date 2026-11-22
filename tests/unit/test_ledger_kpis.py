"""K7 KPI definitions, plan-aware task keys, and questions.form (T50)."""

from copy import deepcopy
from datetime import datetime, timezone
import sys
import tempfile
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "orcexe" / "src"
sys.path.insert(0, str(SOURCE))
from orcexe import ledger  # noqa: E402 — loads task event registration

STAMP = datetime(2026, 11, 16, 21, 30, 0, 123456, tzinfo=timezone.utc)


def metric(value=None, n=0):
    return {"value": value, "status": "measured" if value is not None else "not_seen", "n": n}


class KpiFixture(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.ledger = ledger.Ledger(Path(directory.name) / "ledger.jsonl", clock=lambda: STAMP)

    def append(self, event, task, data=None):
        defaults = {
            "task.classified": dict(mode="existing", lane="fast", signals=[], targets_count=1,
                                    task_sha256="d" * 64, task_preview="preview"),
            "task.dispatched": dict(tool="fake", model="model", effort="low", route_basis="manifest",
                                    branch="task/T1", attempt=1),
            "attempt.started": dict(plan_id="plan-a", run_id="run", attempt=1, contract_sha256="a" * 64,
                                    tool="t", model="m", effort="low"),
            "attempt.ended": dict(run_id="run", status="completed", exit_code=0, duration_ms=1,
                                  usage=dict(input=10, output=10, cached_read=None, cache_write=None,
                                             reasoning=None), warnings=[]),
            "gate.result": dict(run_id="run-1", verdict="accepted", failed_check=None, duration_ms=None),
            "gate.passed": dict(plan_id="plan-a", run_id="run", attempt=1, reason=None),
            "gate.failed": dict(plan_id="plan-a", run_id="run", attempt=1, reason="checks"),
            "task.state": dict(**{"from": "running", "to": "accepted"}, reason="ok"),
            "l1.started": dict(plan_id="plan-a", run_id="run", attempt=1, tool="t", model="m"),
            "questions.form": dict(count=2, form_sha256="b" * 64),
        }
        payload = deepcopy(defaults.get(event, {}))
        if data:
            payload.update(data)
        return self.ledger.append(event, task, payload)


class LedgerKpiTests(KpiFixture):
    def test_empty_ledger_all_not_seen(self):
        names = (
            "orchestration_overhead", "time_to_first_dispatch_ms", "questions_per_task",
            "questions_per_plan", "rounds_per_task", "lane_escalation_rate", "gate_pass_rate",
            "task_acceptance_rate", "l1_calls",
        )
        self.assertEqual(ledger.kpis(self.ledger), {name: metric() for name in names})

    def test_same_task_id_two_plans_do_not_merge(self):
        for plan, run in (("plan-one", "r1"), ("plan-two", "r2")):
            self.append("attempt.started", "T1", dict(plan_id=plan, run_id=run))
            self.append("attempt.ended", "T1", dict(run_id=run))
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["rounds_per_task"], metric(1, 2))

    def test_gate_accepted_then_task_failed(self):
        self.append("task.dispatched", "T1", dict(tool="f", model="m", effort="low", route_basis="m",
                                                branch="b", attempt=1))
        self.append("gate.result", "T1", dict(run_id="do-1", verdict="accepted"))
        self.append("task.state", "T1", dict(plan_id="plan-a", **{"from": "running", "to": "failed"}, reason="x"))
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["gate_pass_rate"], metric(1, 1))
        self.assertEqual(result["task_acceptance_rate"], metric(0, 1))

    def test_cancelled_attempt_stays_in_acceptance_denominator(self):
        self.append("attempt.started", "T1", dict(run_id="c1"))
        self.append("attempt.ended", "T1", dict(run_id="c1", status="cancelled"))
        self.append("task.state", "T1", dict(**{"from": "running", "to": "cancelled"}, reason="user"))
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["task_acceptance_rate"], metric(0, 1))

    def test_blocked_never_dispatched_excluded_from_acceptance(self):
        self.append("task.state", "T9", dict(plan_id="p", **{"from": "pending", "to": "blocked"}, reason="dep"))
        dispatched_only = self.append("attempt.started", "T1")
        _ = dispatched_only
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["task_acceptance_rate"], metric(0, 1))

    def test_gate_passed_final_not_task_accepted(self):
        self.append("attempt.started", "T1")
        self.append("gate.passed", "T1")
        self.append("task.state", "T1", dict(**{"from": "running", "to": "gate_passed"}, reason="iso"))
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["gate_pass_rate"], metric(1, 1))
        self.assertEqual(result["task_acceptance_rate"], metric(0, 1))

    def test_questions_per_plan_dedupes_hash_and_zero_form(self):
        self.append("questions.form", "questions", dict(count=4, form_sha256="a" * 64))
        self.append("questions.form", "questions", dict(count=2, form_sha256="a" * 64))
        self.append("questions.form", "questions", dict(count=0, form_sha256="c" * 64))
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["questions_per_plan"], metric(1, 2))

    def test_l1_calls_measured_zero_when_dispatched(self):
        self.append("attempt.started", "T1")
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["l1_calls"], metric(0, 0))

    def test_l1_calls_not_seen_without_dispatch(self):
        self.append("l1.started", "T1")
        self.assertEqual(ledger.kpis(self.ledger)["l1_calls"], metric(n=1))

    def test_v01_do_ledger_gate_result_only(self):
        self.append("task.classified", "T1", dict(mode="existing", lane="fast", signals=[], targets_count=1,
                                                  task_sha256="d" * 64, task_preview="p"))
        self.append("task.dispatched", "T1", dict(tool="f", model="m", effort="low", route_basis="m",
                                                branch="b", attempt=1))
        self.append("gate.result", "T1", dict(run_id="r1", verdict="accepted"))
        self.append("task.state", "T1", dict(**{"from": "running", "to": "accepted"}, reason="ok"))
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["gate_pass_rate"], metric(1, 1))
        self.assertEqual(result["task_acceptance_rate"], metric(1, 1))
        self.assertEqual(result["questions_per_plan"], metric())

    def test_plan_run_shape_pins_exact_kpis(self):
        shapes = (
            ("contract.frozen", "T1", dict(plan_id="p1", contract_sha256="e" * 64, path="refs/x", head="h1")),
            ("attempt.started", "T1", dict(plan_id="p1", run_id="ra", attempt=1)),
            ("attempt.ended", "T1", dict(run_id="ra")),
            ("gate.passed", "T1", dict(plan_id="p1", run_id="ra", attempt=1)),
            ("task.state", "T1", dict(plan_id="p1", **{"from": "running", "to": "accepted"}, reason="passed")),
            ("attempt.started", "T1", dict(plan_id="p2", run_id="rb", attempt=1)),
            ("attempt.ended", "T1", dict(run_id="rb")),
            ("gate.failed", "T1", dict(plan_id="p2", run_id="rb", attempt=1, reason="project_checks")),
            ("task.state", "T1", dict(plan_id="p2", **{"from": "running", "to": "failed"}, reason="gate")),
            ("task.state", "T3", dict(plan_id="p1", **{"from": "pending", "to": "blocked"}, reason="dep")),
        )
        for event, task, data in shapes:
            self.append(event, task, data)
        result = ledger.kpis(self.ledger)
        self.assertEqual(result, {
            "orchestration_overhead": metric(n=2),
            "time_to_first_dispatch_ms": metric(),
            "questions_per_task": metric(),
            "questions_per_plan": metric(),
            "rounds_per_task": metric(1, 2),
            "lane_escalation_rate": metric(0, 2),
            "gate_pass_rate": metric(0.5, 2),
            "task_acceptance_rate": metric(0.5, 2),
            "l1_calls": metric(0, 0),
        })

    def test_kpi_task_filter_keeps_two_plan_keys(self):
        self.append("attempt.started", "T1", dict(plan_id="pa", run_id="ra"))
        self.append("task.state", "T1", dict(plan_id="pa", **{"from": "running", "to": "accepted"}, reason="ok"))
        self.append("attempt.started", "T1", dict(plan_id="pb", run_id="rb"))
        self.append("task.state", "T1", dict(plan_id="pb", **{"from": "running", "to": "failed"}, reason="no"))
        self.assertEqual(ledger.kpis(self.ledger, task="T1")["task_acceptance_rate"], metric(0.5, 2))

    def test_questions_form_schema_rejects_bool_and_bad_hash(self):
        for data, field in ((dict(count=True, form_sha256="f" * 64), "count"),
                            (dict(count=0, form_sha256="F" * 64), "form_sha256"),
                            (dict(count=0), "form_sha256")):
            with self.subTest(data=data):
                with self.assertRaises(ValueError):
                    self.ledger.append("questions.form", "questions", deepcopy(data))


if __name__ == "__main__":
    unittest.main()