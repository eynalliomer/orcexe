"""Ledger proofs, including read-byte accounting rather than a size timing guess."""

import ast
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import inspect
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest import mock

from ._timing import scaled

SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "orcexe" / "src"
sys.path.insert(0, str(SOURCE))
from orcexe import fsio, gate_lite, ledger
from orcexe.adapters import base


STAMP = datetime(2026, 11, 16, 21, 30, 0, 123456, tzinfo=timezone.utc)
EVENTS = {
    "task.classified": {"mode": "existing", "lane": "fast", "signals": ["docs", "small"],
                        "targets_count": 1, "task_sha256": "a" * 64, "task_preview": "Ömer: düzelt"},
    "task.dispatched": {"tool": "fake", "model": "model", "effort": "low",
                        "route_basis": "manifest", "branch": "task/T1", "attempt": 1},
    "attempt.ended": {"run_id": "run-1", "status": "completed", "exit_code": 0,
                      "duration_ms": 17, "usage": {"input": 80, "cached_read": 20,
                      "cache_write": None, "output": 20, "reasoning": None}, "warnings": ["synthetic"]},
    "gate.result": {"run_id": "run-1", "verdict": "accepted", "failed_check": None, "duration_ms": 4},
    "task.state": {"from": "running", "to": "accepted", "reason": "checks passed"},
    "lane.escalated": {"from_lane": "fast", "to_lane": "standard", "reason": "scope changed"},
    "question.asked": {"count": 1, "topic": "target choice"},
    "orchestrator.usage": {"input": 9, "output": 1, "source": "measured"},
}


def metric(value=None, n=0):
    return {"value": value, "status": "measured" if value is not None else "not_seen", "n": n}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = ledger.default_path(self.directory)
        self.ledger = ledger.Ledger(self.path, clock=lambda: STAMP)

    def append(self, event, task="T1", data=None):
        fields = deepcopy(EVENTS[event])
        if data is not None:
            fields.update(data)
            if isinstance(data.get("usage"), dict):
                # Omitted measurements in fixtures are explicitly unavailable.
                fields["usage"] = dict.fromkeys(EVENTS["attempt.ended"]["usage"])
                fields["usage"].update(data["usage"])
        return self.ledger.append(event, task, fields)

    def rewrite(self, records):
        self.path.write_bytes(b"".join(ledger._serialize(r) + b"\n" for r in records))

    def assert_error(self, line, reason):
        with self.assertRaises(ledger.LedgerError) as caught:
            self.ledger.verify()
        self.assertEqual((caught.exception.line, caught.exception.reason), (line, reason))
        self.assertIn("line {}:".format(line), str(caught.exception))

    def test_round_trip_every_event_and_exact_fsio_serialization(self):
        expected = [self.append(event) for event in EVENTS]
        other = self.directory / "fsio.jsonl"
        for record in expected:
            fsio.append_jsonl(other, record)
        self.assertEqual(self.path.read_bytes(), other.read_bytes())
        self.assertEqual(list(self.ledger.records()), expected)
        self.assertEqual(self.ledger.verify(), {"records": 8, "last_seq": 8, "ok": True})
        previous = "0" * 64
        for number, raw in enumerate(self.path.read_bytes().splitlines(), 1):
            record = json.loads(raw)
            self.assertEqual(record["seq"], number)
            self.assertEqual(record["prev"], previous)
            self.assertEqual(record["ts"], "2026-11-16T21:30:00.123Z")
            previous = hashlib.sha256(raw).hexdigest()

    def test_unknown_values_are_dropped_by_name_at_each_depth(self):
        data = {"status": "failed", "mystery": {"never": "write this value"}, "_dropped": ["forged"],
                "usage": {"input": 1, "output": None, "unknown": "hidden"}}
        result = self.append("attempt.ended", data=data)["data"]
        expected = deepcopy(EVENTS["attempt.ended"])
        expected.update(status="failed", _dropped=["_dropped", "mystery"],
                        usage=dict.fromkeys(expected["usage"]))
        expected["usage"].update(input=1, output=None, _dropped=["unknown"])
        self.assertEqual(result, expected)
        self.assertNotIn(b"write this value", self.path.read_bytes())
        self.assertNotIn(b"forged", self.path.read_bytes())

    def test_invalid_event_task_and_data_fail_before_creating_file(self):
        for event in ("unknown", "", None, [], 3):
            with self.subTest(event=event), self.assertRaises(ValueError):
                self.ledger.append(event, "T1", {})
        for task in ("", "a" * 65, "bad/id", "has space", "é", "a\n", None, []):
            with self.subTest(task=task), self.assertRaises(ValueError):
                self.ledger.append("task.state", task, {})
        with self.assertRaises(TypeError):
            self.ledger.append("task.state", "T1", [])
        with self.assertRaises(ValueError):
            self.append("task.state", "sk-" + "A9bC" * 7)
        self.assertFalse(self.path.exists())
        self.append("task.state", "A" + "._-9" * 15)

    def test_strict_types_nulls_and_lists(self):
        invalid = {"input": True, "output": -1, "source": "guessed"}
        with self.assertRaisesRegex(ValueError, "input"):
            self.append("orchestrator.usage", data=invalid)
        for value in (0, -1, True, 1.0, "1", None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "count"):
                self.append("question.asked", data={"count": value})
        with self.assertRaisesRegex(ValueError, "warnings"):
            self.append("attempt.ended", data={"warnings": [{"nested": "no"}]})
        result = self.append("attempt.ended", data={"exit_code": -9, "duration_ms": None,
                             "usage": {"input": None, "output": 0}})
        self.assertEqual(result["data"]["exit_code"], -9)
        self.assertIsNone(result["data"]["duration_ms"])
        self.assertEqual(result["data"]["usage"],
                         dict(input=None, output=0, cached_read=None, cache_write=None, reasoning=None))
        for value in ([1], "not a list"):
            with self.assertRaisesRegex(ValueError, "signals"):
                self.append("task.classified", data={"signals": value})
        self.assertEqual(self.append("task.classified", data={"signals": ["id"] * 129})["data"]["signals"],
                         ["id"] * 129)
        self.ledger.verify()

    def test_invalid_known_fields_table_leaves_file_byte_for_byte_unchanged(self):
        numbers = (True, False, -1, 2**53, 10**30, 1.0, "1", [], {},
                   float("nan"), float("inf"), -float("inf"))
        text = (None, True, 1, 1.5, [], {})
        lists = (None, True, 1, "text", {}, [1], [None], [{}])
        enums = (None, True, 1, [], {}, "", "unknown", "FAST", "fast\n")
        table = {
            "task.classified": {
                "mode": enums + ("auto", "Existing"), "lane": enums + ("safe",),
                "signals": lists, "targets_count": numbers + (None,),
                "task_sha256": text + ("", "a" * 63, "a" * 65, "A" * 64, "g" * 64, "a" * 64 + "\n"),
                "task_preview": text,
            },
            "task.dispatched": {**dict.fromkeys(("tool", "model", "effort", "route_basis", "branch"), text),
                                "attempt": numbers + (None,)},
            "attempt.ended": {
                "run_id": text, "status": enums + ("ok", "success"),
                "exit_code": (True, False, -256, 256, 2**53, -2**53, 1.0, "1", [], {},
                              float("nan"), float("inf"), -float("inf")),
                "duration_ms": numbers, "usage": (None, True, [], "text", 1), "warnings": lists,
                **dict.fromkeys(("usage." + key for key in EVENTS["attempt.ended"]["usage"]), numbers),
            },
            "gate.result": {"run_id": text, "verdict": enums + ("rejected", "completed"),
                            "failed_check": text[1:], "duration_ms": numbers},
            "task.state": dict.fromkeys(("from", "to", "reason"), text),
            "lane.escalated": {"from_lane": enums, "to_lane": enums, "reason": text},
            "question.asked": {"count": numbers + (None, 0), "topic": text},
            "orchestrator.usage": {"input": numbers, "output": numbers, "source": enums + ("guessed",)},
        }
        self.append("task.state")
        before = self.path.read_bytes()
        absent = self.directory / "absent" / "ledger.jsonl"
        for event, fields in table.items():
            for field, invalid in fields.items():
                for index, value in enumerate(invalid):
                    with self.subTest(event=event, field=field, case=index):
                        data = deepcopy(EVENTS[event])
                        target = data
                        parts = field.split(".")
                        for part in parts[:-1]:
                            target = target[part]
                        target[parts[-1]] = value
                        for writer in (self.ledger, ledger.Ledger(absent)):
                            with self.assertRaises(ValueError) as caught:
                                writer.append(event, "T1", data)
                            self.assertEqual(str(caught.exception), "invalid data." + field)
                        self.assertEqual(self.path.read_bytes(), before)
                        self.assertFalse(absent.parent.exists())

    def test_every_documented_field_is_required_including_nullable_measurements(self):
        self.append("task.state")
        before = self.path.read_bytes()
        absent = self.directory / "absent" / "ledger.jsonl"
        for event, fields in EVENTS.items():
            paths = [(key,) for key in fields]
            if event == "attempt.ended":
                paths += [("usage", key) for key in fields["usage"]]
            for parts in paths:
                with self.subTest(event=event, field=".".join(parts)):
                    data = deepcopy(fields)
                    target = data
                    for part in parts[:-1]:
                        target = target[part]
                    del target[parts[-1]]
                    for writer in (self.ledger, ledger.Ledger(absent)):
                        with self.assertRaises(ValueError) as caught:
                            writer.append(event, "T1", data)
                        self.assertEqual(str(caught.exception), "missing data." + ".".join(parts))
                    self.assertEqual(self.path.read_bytes(), before)
                    self.assertFalse(absent.parent.exists())

    def test_integer_boundaries_and_nullable_fields_round_trip(self):
        fields = [("task.classified", "targets_count"), ("task.dispatched", "attempt"),
                  ("question.asked", "count"), ("orchestrator.usage", "input"),
                  ("orchestrator.usage", "output"), ("attempt.ended", "duration_ms"),
                  ("gate.result", "duration_ms")]
        fields += [("attempt.ended", "usage." + key) for key in EVENTS["attempt.ended"]["usage"]]
        for event, field in fields:
            values = (1 if field == "count" else 0, 2**53 - 1)
            if event in ("attempt.ended", "gate.result", "orchestrator.usage"):
                values += (None,)
            for value in values:
                with self.subTest(event=event, field=field, value=value):
                    data = deepcopy(EVENTS[event])
                    parts = field.split(".")
                    target = data
                    for part in parts[:-1]:
                        target = target[part]
                    target[parts[-1]] = value
                    result = self.ledger.append(event, "T1", data)
                    self.assertEqual(result["data"], data)
        for value in (-255, -9, 0, 255, None):
            result = self.append("attempt.ended", data={"exit_code": value})
            self.assertEqual(result["data"]["exit_code"], value)
        self.assertIsNone(self.append("gate.result", data={"failed_check": None})["data"]["failed_check"])
        self.ledger.verify()

    def test_closed_vocabularies_and_sha256_valid_values(self):
        table = (("task.classified", "mode", ("existing", "new", "everyday")),
                 ("task.classified", "lane", ("fast", "standard", "risky")),
                 ("lane.escalated", "from_lane", ("fast", "standard", "risky")),
                 ("lane.escalated", "to_lane", ("fast", "standard", "risky")),
                 ("attempt.ended", "status", ("completed", "failed", "quota", "auth", "timeout",
                                             "stalled", "blocked", "cancelled")),
                 ("gate.result", "verdict", ("accepted", "needs_fix", "escalated", "failed", "unverified")),
                 ("orchestrator.usage", "source", ("measured", "not_seen")),
                 ("task.classified", "task_sha256", ("0" * 64, "0123456789abcdef" * 4)))
        for event, field, values in table:
            for value in values:
                with self.subTest(event=event, field=field, value=value):
                    self.assertEqual(self.append(event, data={field: value})["data"][field], value)
        self.ledger.verify()

    def test_copied_run_and_gate_vocabularies_match_the_authoritative_modules(self):
        self.assertEqual(ledger._STATUSES, base._STATUSES)
        # Gate's vocabulary is an inline set, not a public module constant.
        tree = ast.parse(inspect.getsource(gate_lite.validate_gate_result))
        sets = [ast.literal_eval(node.comparators[0]) for node in ast.walk(tree)
                if isinstance(node, ast.Compare) and isinstance(node.left, ast.Subscript)
                and isinstance(node.left.value, ast.Name) and node.left.value.id == "d"
                and isinstance(node.left.slice, ast.Constant) and node.left.slice.value == "verdict"
                and len(node.ops) == 1 and isinstance(node.ops[0], ast.In)]
        self.assertEqual(sets, [ledger._VERDICTS])

    def test_seq_integer_bound_and_overflow_never_write(self):
        record = self.append("task.state")
        for value in (0, -1, True, 1.0, 2**53):
            with self.subTest(seq=value):
                self.rewrite([dict(record, seq=value)])
                self.assert_error(1, "seq")
        self.rewrite([dict(record, seq=2**53 - 1)])
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "seq"):
            self.append("task.state")
        self.assertEqual(self.path.read_bytes(), before)

    def test_all_free_text_fields_and_list_entries_are_redacted(self):
        secret = "gh" + "p_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
        fields = {
            "task.classified": ("task_preview", "signals"),
            "task.dispatched": ("tool", "model", "effort", "route_basis", "branch"),
            "attempt.ended": ("run_id", "warnings"),
            "gate.result": ("run_id", "failed_check"),
            "task.state": ("from", "to", "reason"),
            "lane.escalated": ("reason",), "question.asked": ("topic",),
        }
        for event, names in fields.items():
            for name in names:
                with self.subTest(event=event, field=name):
                    value = secret if name not in ("signals", "warnings") else [secret]
                    result = self.append(event, data={name: value})["data"][name]
                    self.assertIn("[REDACTED", result[0] if isinstance(result, list) else result)
        self.append("attempt.ended", data={secret: {"secret": secret},
                    "usage": {secret: secret, "unknown": float("nan"), "other": object()}})
        self.assertNotIn(secret.encode(), self.path.read_bytes())
        self.ledger.verify()

    def test_no_provider_secret_in_any_carrier_and_redact_before_truncation(self):
        secret = "sk-" + "proj-" + "Fake19Ab_" * 8
        data = {"task_preview": "x" * 190 + " " + secret, "unknown": secret,
                secret: secret}
        clean = self.append("task.classified", data=data)["data"]
        self.assertLessEqual(len(clean["task_preview"]), 200)
        self.assertIn("[REDACT", clean["task_preview"])
        self.append("task.dispatched", data={"model": json.dumps({"nested": {"token": secret}})})
        for event in ("task.state", "lane.escalated"):
            self.append(event, data={"reason": secret})
        self.append("question.asked", data={"topic": secret, "count": 1})
        self.append("attempt.ended", data={"warnings": [secret], "usage": {"unknown": {"deep": secret}}})
        for event, field in (("task.dispatched", "branch"), ("gate.result", "failed_check")):
            self.append(event, data={field: secret})
        raw = self.path.read_bytes()
        self.assertNotIn(secret.encode(), raw)
        self.assertNotIn(secret[:30].encode(), raw)
        self.assertNotIn(secret, json.dumps(list(self.ledger.records())))
        self.ledger.verify()

    def test_every_text_field_is_bounded_and_unicode_is_preserved(self):
        for event, data in (("task.state", {"reason": "ö" * 300}),
                            ("lane.escalated", {"reason": "ö" * 300}),
                            ("question.asked", {"topic": "ö" * 100}),
                            ("attempt.ended", {"warnings": ["ö" * 300]}),
                            ("task.dispatched", {"branch": "ö" * 300})):
            clean = self.append(event, data=data)["data"]
            for key in data:
                value = clean[key]
                for text in value if isinstance(value, list) else [value]:
                    self.assertEqual(text, "ö" * (80 if key == "topic" else 200))
        text = "Türkçe\u2028bir\u2029iki\u0085üç\x0cdört\nbeş\raltı"
        self.append("task.state", data={"reason": text})
        self.append("task.state", data={"reason": "son"})
        raw = self.path.read_bytes()
        for separator in ("\u2028", "\u2029", "\u0085", "\x0c"):
            self.assertNotIn(separator.encode(), raw)
        self.assertIn("Türkçe".encode(), raw)
        self.assertEqual(list(self.ledger.records())[-2]["data"]["reason"], text)
        self.ledger.verify()

    def test_clock_utc_conversion_equal_timestamps_and_regression(self):
        self.ledger.clock = lambda: STAMP.astimezone(timezone(timedelta(hours=3)))
        first = self.append("task.state")
        second = self.append("task.state")
        self.assertEqual(first["ts"], second["ts"])
        before = self.path.read_bytes()
        self.ledger.clock = lambda: STAMP - timedelta(milliseconds=1)
        with self.assertRaisesRegex(ledger.LedgerError, "clock_regression"):
            self.append("task.state")
        self.assertEqual(self.path.read_bytes(), before)
        self.ledger.clock = lambda: STAMP.replace(tzinfo=None)
        with self.assertRaises(ValueError):
            self.append("task.state")
        self.assertEqual(self.path.read_bytes(), before)

    def test_modes_and_empty_missing_ledgers(self):
        self.assertEqual(self.path, self.directory / ".orcexe" / "ledger.jsonl")
        self.assertEqual(self.ledger.verify(), {"records": 0, "last_seq": 0, "ok": True})
        self.assertEqual(list(self.ledger.records()), [])
        self.assertFalse(self.path.exists())
        self.append("task.state")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(self.path.parent.stat().st_mode), 0o700)
        self.path.write_bytes(b"")
        self.assertEqual(self.append("task.state")["seq"], 1)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits")
    def test_created_file_and_each_parent_have_private_modes(self):
        self.path = self.directory / "nested" / "main" / ".orcexe" / "ledger.jsonl"
        self.ledger = ledger.Ledger(self.path, clock=lambda: STAMP)
        previous_umask = os.umask(0)
        try:
            self.append("task.state")
        finally:
            os.umask(previous_umask)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        for parent in (self.path.parent, self.path.parent.parent, self.path.parent.parent.parent):
            self.assertEqual(stat.S_IMODE(parent.stat().st_mode), 0o700)

    def test_windows_byte_lock_retries_contention_and_unlocks_on_error(self):
        fake = SimpleNamespace(LK_NBLCK=2, LK_UNLCK=0)
        calls = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("w+b") as handle:
            fd = handle.fileno()
            def locking(locked_fd, mode, size):
                self.assertEqual((locked_fd, size), (fd, 1))
                self.assertEqual(os.lseek(fd, 0, os.SEEK_CUR), 0)
                calls.append(mode)
                if len(calls) == 1:
                    raise PermissionError(ledger.errno.EACCES, "synthetic contention")
            fake.locking = locking
            with mock.patch.dict(sys.modules, {"msvcrt": fake}), \
                    mock.patch.object(ledger.os, "name", "nt"), \
                    mock.patch.object(ledger.time, "sleep") as sleep:
                with self.assertRaisesRegex(RuntimeError, "synthetic body"):
                    with ledger._lock(fd):
                        os.lseek(fd, 5, os.SEEK_SET)
                        raise RuntimeError("synthetic body")
                sleep.assert_called_once()
            self.assertEqual(calls, [fake.LK_NBLCK, fake.LK_NBLCK, fake.LK_UNLCK])

    def test_middle_tampering_edit_delete_swap_duplicate_prev_timestamp(self):
        original = [self.append("task.state", data={"reason": str(i)}) for i in range(4)]
        for mutation, line, reason in (("edit", 3, "prev"), ("delete", 2, "seq"),
                                       ("swap", 2, "seq"), ("duplicate", 3, "seq"),
                                       ("prev", 2, "prev"), ("ts", 2, "ts_regression")):
            with self.subTest(mutation=mutation):
                records = json.loads(json.dumps(original))
                if mutation == "edit":
                    records[1]["data"]["reason"] = "edited"
                elif mutation == "delete":
                    del records[1]
                elif mutation == "swap":
                    records[1], records[2] = records[2], records[1]
                elif mutation == "duplicate":
                    records.insert(2, records[1])
                elif mutation == "prev":
                    records[1]["prev"] = "f" * 64
                else:
                    records[1]["ts"] = "2026-11-16T21:30:00.122Z"
                self.rewrite(records)
                self.assert_error(line, reason)

    def test_structural_corruption_has_safe_line_reasons(self):
        original = self.append("task.state")
        for key, value, reason in (("schema", "future", "schema"), ("seq", True, "seq"),
                                   ("event", "future", "event"), ("task", "bad/id", "task"),
                                   ("prev", "x", "prev"), ("prev", "f" * 64, "prev"),
                                   ("ts", "2026-02-30T00:00:00.000Z", "ts"),
                                   ("data", {"reason": {"nested": "bad"}}, "data")):
            with self.subTest(key=key, value=value):
                record = dict(original, **{key: value})
                self.rewrite([record])
                self.assert_error(1, reason)
        self.path.write_bytes(b"{bad secret text}\n")
        self.assert_error(1, "invalid_json")
        self.path.write_bytes(b"\xff\n")
        self.assert_error(1, "invalid_json")

    def test_crash_tail_refusal_never_changes_bytes(self):
        original = self.append("task.state")
        prefix = ledger._serialize(original) + b"\n"
        for broken in (prefix[:-1], prefix + b'{"seq":2', prefix + b'{partial}\n', prefix + b"\n"):
            with self.subTest(tail=broken[-10:]):
                self.path.write_bytes(broken)
                with self.assertRaises(ledger.LedgerError) as caught:
                    self.append("task.state")
                self.assertEqual(str(caught.exception), "truncated_tail")
                self.assertEqual(self.path.read_bytes(), broken)
        self.path.write_bytes(prefix + b"{")
        self.assert_error(2, "truncated_tail")

    def test_filtered_stream_still_verifies_every_line(self):
        self.append("task.state", "T1")
        self.append("question.asked", "T2")
        self.append("task.state", "T2")
        self.assertEqual(len(list(self.ledger.records(task="T2"))), 2)
        self.assertEqual(len(list(self.ledger.records(event="task.state"))), 2)
        self.assertEqual(len(list(self.ledger.records(task="T2", event="task.state"))), 1)
        raw = self.path.read_bytes().splitlines(keepends=True)
        self.path.write_bytes(raw[0] + raw[2])
        with self.assertRaises(ledger.LedgerError):
            list(self.ledger.records(task="missing", event="gate.result"))

    def test_append_uses_one_write_append_flag_and_fsync(self):
        real_open, real_write, real_sync = os.open, os.write, os.fsync
        events = []
        def write(fd, value):
            events.append("write")
            return real_write(fd, value)
        def sync(fd):
            events.append("fsync")
            return real_sync(fd)
        with mock.patch.object(ledger.os, "open", wraps=real_open) as opened, \
                mock.patch.object(ledger.os, "write", side_effect=write) as written, \
                mock.patch.object(ledger.os, "fsync", side_effect=sync):
            self.append("task.state")
        self.assertEqual(events, ["write", "fsync"])
        self.assertEqual(written.call_count, 1)
        self.assertTrue(opened.call_args.args[1] & os.O_APPEND)
        self.assertEqual(opened.call_args.args[2], 0o600)

    def test_partial_write_is_not_retried_or_repaired(self):
        self.append("task.state")
        real_write = os.write
        with mock.patch.object(ledger.os, "write", side_effect=lambda fd, raw: real_write(fd, raw[:5])) as written:
            with self.assertRaisesRegex(ledger.LedgerError, "short_write"):
                self.append("task.state")
        self.assertEqual(written.call_count, 1)
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ledger.LedgerError, "truncated_tail"):
            self.append("task.state")
        self.assertEqual(before, self.path.read_bytes())

    def test_tail_read_is_bounded_independently_of_file_size(self):
        # Sparse 50 MiB prefix; instrument both os.read and builtins.open.
        # Prefix need not be valid: append promises tail lookup, not full verify.
        record = self.append("task.state")
        tail = ledger._serialize(record) + b"\n"
        for size in (0, 50 * 1024 * 1024):
            with self.subTest(size=size):
                with self.path.open("wb") as handle:
                    handle.seek(size)
                    if size:
                        handle.write(b"\n")
                    handle.write(tail)
                real_read = os.read
                amounts = []
                def read(fd, amount):
                    amounts.append(amount)
                    return real_read(fd, amount)
                with mock.patch.object(ledger.os, "read", side_effect=read), \
                        mock.patch("builtins.open", side_effect=AssertionError("unbounded open")):
                    self.append("task.state")
                self.assertLessEqual(sum(amounts), 4097)
                with self.path.open("rb") as handle:
                    handle.seek(-1, os.SEEK_END)
                    self.assertEqual(handle.read(1), b"\n")

    def test_tail_larger_than_one_block_is_hashed_exactly(self):
        self.append("attempt.ended", data={"warnings": ["ö" * 200] * 100})
        previous = hashlib.sha256(self.path.read_bytes()[:-1]).hexdigest()
        self.assertEqual(self.append("task.state")["prev"], previous)
        self.ledger.verify()

    def test_four_processes_append_without_interleaving(self):
        script = '''
import sys
sys.path.insert(0, sys.argv[1])
from orcexe.ledger import Ledger
ledger = Ledger(sys.argv[2])
for i in range(200):
    ledger.append("task.state", sys.argv[3], {"from": "running", "to": "accepted", "reason": "synthetic"})
'''
        children = []
        try:
            for i in range(4):
                children.append(subprocess.Popen([sys.executable, "-B", "-c", script, str(SOURCE),
                                                 str(self.path), "T" + str(i)],
                                                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            deadline = time.monotonic() + scaled(5)
            for child in children:
                output, errors = child.communicate(timeout=max(scaled(.001), deadline - time.monotonic()))
                self.assertEqual(child.returncode, 0, errors.decode())
                self.assertEqual(output, b"")
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()
        self.assertEqual(self.ledger.verify(), {"records": 800, "last_seq": 800, "ok": True})
        self.assertEqual([r["seq"] for r in self.ledger.records()], list(range(1, 801)))

    def test_verify_100000_records_within_scaled_four_seconds(self):
        # Bulk fixture creation avoids timing 100k fsyncs; every link is real.
        self.path.parent.mkdir()
        record = {"schema": "orcexe.ledger.v1", "seq": 1, "ts": "2026-11-16T21:30:00.123Z",
                  "event": "task.state", "task": "T1", "data": dict(EVENTS["task.state"]), "prev": "0" * 64}
        with self.path.open("wb") as handle:
            for number in range(1, 100001):
                record["seq"] = number
                raw = ledger._serialize(record)
                handle.write(raw + b"\n")
                record["prev"] = hashlib.sha256(raw).hexdigest()
        start = time.monotonic()
        self.assertEqual(self.ledger.verify(), {"records": 100000, "last_seq": 100000, "ok": True})
        # Keep verification of every record in the default run. Only the
        # megabyte-throughput clock assertion requires an idle slow-suite job.
        if os.environ.get('ORCEXE_SLOW_TESTS') != '1':
            self.skipTest('ledger throughput timing requires ORCEXE_SLOW_TESTS=1; 100000 records verified')
        self.assertLess(time.monotonic() - start, scaled(4))

    def test_kpi_empty_population_is_not_seen(self):
        self.assertEqual(ledger.kpis(self.ledger), {name: metric() for name in (
            "orchestration_overhead", "time_to_first_dispatch_ms", "questions_per_task",
            "rounds_per_task", "lane_escalation_rate", "acceptance_rate")})

    def test_kpi_table_even_median_and_per_task(self):
        for task in ("T1", "T2", "T3", "T4"):
            self.append("task.classified", task)
        self.append("question.asked", "T1", {"count": 2})
        self.append("question.asked", "T3", {"count": 4})
        for index, task in enumerate(("T1", "T2", "T3", "T4")):
            for _ in range(index):
                self.append("attempt.ended", task)
        self.append("lane.escalated", "T3")
        self.append("lane.escalated", "T3")
        self.append("gate.result", "T1")
        self.append("gate.result", "T2", {"verdict": "accepted"})
        self.append("gate.result", "T2", {"verdict": "needs_fix"})
        self.append("orchestrator.usage", "T2", {"input": 30, "output": 30, "source": "measured"})
        self.assertEqual(ledger.kpis(self.ledger), {
            "orchestration_overhead": metric(.1, 6), "questions_per_task": metric(1.5, 4),
            "time_to_first_dispatch_ms": metric(),
            "rounds_per_task": metric(1.5, 4), "lane_escalation_rate": metric(.25, 4),
            "acceptance_rate": metric(.5, 2)})
        self.assertEqual(ledger.kpis(self.ledger, task="T2"), {
            "orchestration_overhead": metric(.6, 1), "questions_per_task": metric(0, 1),
            "time_to_first_dispatch_ms": metric(),
            "rounds_per_task": metric(1, 1), "lane_escalation_rate": metric(0, 1),
            "acceptance_rate": metric(0, 1)})
        self.assertTrue(all(v["status"] == "not_seen" for v in ledger.kpis(self.ledger, task="absent").values()))

    def test_kpi_fast_lane_no_questions_is_measured_zero(self):
        self.append("task.classified")
        result = ledger.kpis(self.ledger, task="T1")
        self.assertEqual(result["questions_per_task"], metric(0, 1))
        self.assertEqual(result["rounds_per_task"], metric(0, 1))
        self.assertEqual(result["orchestration_overhead"], metric())
        self.assertEqual(result["acceptance_rate"], metric())

    def test_time_to_first_dispatch_uses_first_events_and_two_task_median(self):
        events = ((0, "task.classified", "T1"), (1, "task.classified", "T2"),
                  (2, "task.classified", "T1"), (4, "task.dispatched", "T1"),
                  (5, "task.classified", "T2"), (6, "task.dispatched", "T2"),
                  (7, "task.dispatched", "T1"), (8, "task.dispatched", "T2"),
                  (9, "task.classified", "classified_only"),
                  (10, "task.dispatched", "dispatched_only"))
        for milliseconds, event, task in events:
            self.ledger.clock = lambda ms=milliseconds: STAMP + timedelta(milliseconds=ms)
            self.append(event, task)
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["time_to_first_dispatch_ms"], metric(4.5, 2))
        self.assertEqual(ledger.kpis(self.ledger, task="T1")["time_to_first_dispatch_ms"], metric(4, 1))
        self.assertEqual(ledger.kpis(self.ledger, task="T2")["time_to_first_dispatch_ms"], metric(5, 1))
        for task in ("classified_only", "dispatched_only", "absent"):
            self.assertEqual(ledger.kpis(self.ledger, task=task)["time_to_first_dispatch_ms"], metric())

    def test_time_to_first_dispatch_not_seen_without_a_paired_task(self):
        self.assertEqual(ledger.kpis(self.ledger)["time_to_first_dispatch_ms"], metric())
        self.append("task.classified", "T1")
        self.append("task.dispatched", "T2")
        self.assertEqual(ledger.kpis(self.ledger)["time_to_first_dispatch_ms"], metric())

    def test_time_to_first_dispatch_zero_and_dispatch_before_classification(self):
        self.append("task.classified", "T1")
        self.append("task.dispatched", "T1")
        self.assertEqual(ledger.kpis(self.ledger)["time_to_first_dispatch_ms"], metric(0, 1))
        self.append("task.dispatched", "T2")
        self.ledger.clock = lambda: STAMP + timedelta(days=2, milliseconds=3)
        self.append("task.classified", "T2")
        self.assertEqual(ledger.kpis(self.ledger, task="T2")["time_to_first_dispatch_ms"], metric(-172800003, 1))

    def test_overhead_not_seen_cases_and_measured_zero(self):
        cases = [([], [{"input": 1, "output": 2}], None),
                 ([{"input": 1, "output": 2, "source": "measured"}], [{"input": None, "output": 2}], None),
                 ([{"input": 1, "output": 2, "source": "measured"}], [{"input": 0, "output": 0}], None),
                 ([{"input": 1, "output": 2, "source": "not_seen"}], [{"input": 1, "output": 2}], None),
                 ([{"input": None, "output": 2, "source": "measured"}], [{"input": 1, "output": 2}], None),
                 ([{"input": 1, "output": 2, "source": "measured"}], [{"output": 2}], None),
                 ([{"input": 0, "output": 0, "source": "measured"}], [{"input": 1, "output": 2}], 0),
                 ([{"input": 1, "output": 2, "source": "measured"}], [], None)]
        for index, (samples, usages, expected) in enumerate(cases):
            with self.subTest(case=index):
                task = "T" + str(index)
                for usage in usages:
                    self.append("attempt.ended", task, {"usage": usage})
                for sample in samples:
                    self.append("orchestrator.usage", task, sample)
                self.assertEqual(ledger.kpis(self.ledger, task=task)["orchestration_overhead"], metric(expected, len(usages)))

    def test_overhead_partial_samples_do_not_produce_partial_totals(self):
        self.append("attempt.ended")
        self.append("attempt.ended", data={"usage": {"input": 10, "output": None}})
        self.append("orchestrator.usage")
        self.assertEqual(ledger.kpis(self.ledger)["orchestration_overhead"], metric(None, 2))

    def test_missing_counts_and_verdicts_never_become_measured_zero(self):
        self.append("task.classified")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "count"):
            self.append("question.asked", data={"count": "invalid"})
        with self.assertRaisesRegex(ValueError, "verdict"):
            self.ledger.append("gate.result", "T1", {"run_id": "run"})
        self.assertEqual(self.path.read_bytes(), before)
        result = ledger.kpis(self.ledger)
        self.assertEqual(result["questions_per_task"], metric(0, 1))
        self.assertEqual(result["acceptance_rate"], metric())
        self.append("gate.result", data={"verdict": "accepted"})
        self.assertEqual(ledger.kpis(self.ledger)["acceptance_rate"], metric(1, 1))

    def test_attempts_without_classification_are_observed_rounds(self):
        self.append("attempt.ended")
        self.append("attempt.ended")
        result = ledger.kpis(self.ledger, task="T1")
        self.assertEqual(result["rounds_per_task"], metric(2, 1))
        self.assertEqual(result["questions_per_task"], metric())
        self.assertEqual(result["lane_escalation_rate"], metric())

    def test_kpis_propagate_verification_failure(self):
        self.append("task.classified")
        self.path.write_bytes(self.path.read_bytes()[:-1])
        with self.assertRaises(ledger.LedgerError):
            ledger.kpis(self.ledger)


if __name__ == "__main__":
    unittest.main()
