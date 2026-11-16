"""Verify atomic state replacement and append-only JSON records."""

import json
import os
from pathlib import Path
import select
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


SOURCE = Path(__file__).resolve().parents[2] / "plugins" / "orcexe" / "src"
sys.path.insert(0, str(SOURCE))

from orcexe import fsio


class FsioTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.path = self.directory / "state.json"

    def test_atomic_text_creates_parents_and_uses_same_directory(self):
        path = self.directory / "nested" / "state.txt"
        with mock.patch.object(fsio.tempfile, "mkstemp", wraps=tempfile.mkstemp) as create:
            fsio.atomic_write_text(path, "Ömer\nğüşıöç\n")
        self.assertEqual(Path(create.call_args.kwargs["dir"]), path.parent)
        self.assertEqual(path.read_text(encoding="utf-8"), "Ömer\nğüşıöç\n")
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_atomic_write_flushes_syncs_chmods_then_replaces(self):
        events = []
        real_fsync = os.fsync
        real_chmod = os.chmod
        real_replace = os.replace

        def sync(fd):
            # Seeing every byte through a separate descriptor proves flush ran.
            temporary = next(self.directory.iterdir())
            self.assertEqual(temporary.read_text(encoding="utf-8"), "complete")
            events.append("fsync")
            real_fsync(fd)

        def chmod(path, mode):
            events.append("chmod")
            real_chmod(path, mode)

        def replace(source, destination):
            events.append("replace")
            real_replace(source, destination)

        with mock.patch.object(fsio.os, "fsync", side_effect=sync), \
                mock.patch.object(fsio.os, "chmod", side_effect=chmod), \
                mock.patch.object(fsio.os, "replace", side_effect=replace):
            fsio.atomic_write_text(self.path, "complete")
        self.assertEqual(events, ["fsync", "chmod", "replace"])

    def test_normal_failures_preserve_target_and_remove_temporary(self):
        self.path.write_text("old complete content", encoding="utf-8")
        for operation in ("fdopen", "fsync", "chmod", "replace"):
            with self.subTest(operation=operation):
                with mock.patch.object(fsio.os, operation, side_effect=OSError("injected failure")):
                    with self.assertRaises(OSError):
                        fsio.atomic_write_text(self.path, "new complete content")
                self.assertEqual(self.path.read_text(encoding="utf-8"), "old complete content")
                self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_write_failure_removes_partial_temporary(self):
        self.path.write_text("old", encoding="utf-8")
        real_fdopen = os.fdopen

        def fail_write(fd, *args, **kwargs):
            handle = real_fdopen(fd, *args, **kwargs)
            original_write = handle.write

            def write(text):
                original_write(text[:2])
                raise OSError("injected partial write")

            handle.write = write
            return handle

        with mock.patch.object(fsio.os, "fdopen", side_effect=fail_write):
            with self.assertRaises(OSError):
                fsio.atomic_write_text(self.path, "new complete content")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "old")
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    @unittest.skipIf(os.name == "nt", "SIGKILL requires POSIX")
    def test_killed_writer_leaves_old_or_new_full_content(self):
        old = "old complete content"
        new = "N" * (4 * 1024 * 1024)
        self.path.write_text(old, encoding="utf-8")
        script = r'''
import os
import sys
import time
sys.path.insert(0, sys.argv[1])
from orcexe import fsio
real_fdopen = os.fdopen
def slow_fdopen(fd, *args, **kwargs):
    handle = real_fdopen(fd, *args, **kwargs)
    original_write = handle.write
    def write(text):
        original_write(text[:4096])
        handle.flush()
        sys.stdout.write("ready\n")
        sys.stdout.flush()
        time.sleep(30)
        return original_write(text[4096:])
    handle.write = write
    return handle
fsio.os.fdopen = slow_fdopen
fsio.atomic_write_text(sys.argv[2], "N" * (4 * 1024 * 1024))
'''
        with subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(SOURCE), str(self.path)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ) as process:
            try:
                ready, _, _ = select.select([process.stdout], [], [], 10)
                self.assertTrue(ready, "Child did not reach its partial write")
                self.assertEqual(process.stdout.readline(), b"ready\n")
                os.kill(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
                self.assertEqual(process.returncode, -signal.SIGKILL)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.communicate(timeout=10)
        self.assertTrue(self.path.read_text(encoding="utf-8") in (old, new))

    @unittest.skipIf(os.name == "nt", "POSIX permission bits required")
    def test_atomic_modes_are_honoured_on_create_and_replace(self):
        for writer, value in ((fsio.atomic_write_text, "text"), (fsio.atomic_write_json, {})):
            for mode in (0o600, 0o640, 0o644):
                with self.subTest(writer=writer.__name__, mode=oct(mode)):
                    writer(self.path, value, mode=mode)
                    self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), mode)

    def test_json_round_trip_and_exact_format(self):
        obj = {"z": "ğüşıöç", "name": "Ömer"}
        fsio.atomic_write_json(self.path, obj)
        self.assertEqual(fsio.read_json(self.path), obj)
        self.assertEqual(self.path.read_bytes(), '{\n  "name": "Ömer",\n  "z": "ğüşıöç"\n}\n'.encode("utf-8"))

    def test_missing_json_default_and_no_default(self):
        default = []
        self.assertIs(fsio.read_json(self.path, default), default)
        self.assertIsNone(fsio.read_json(self.path, None))
        with self.assertRaises(FileNotFoundError):
            fsio.read_json(self.path)

    def test_invalid_json_includes_path_even_with_default(self):
        self.path.write_text("{invalid}", encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            fsio.read_json(self.path, {})
        self.assertIn(str(self.path), str(caught.exception))
        self.assertNotIn("{invalid}", str(caught.exception))

    def test_jsonl_appends_one_line_with_unicode_and_escaped_newlines(self):
        path = self.directory / "nested" / "ledger.jsonl"
        objects = [{"z": "ğüşıöç\nsecond", "name": "Ömer"}, {"b": 2, "a": 1}]
        real_open = os.open
        with mock.patch.object(fsio.os, "open", wraps=real_open) as opened:
            for obj in objects:
                fsio.append_jsonl(path, obj)
        for call in opened.call_args_list:
            self.assertTrue(call.args[1] & os.O_APPEND)
            self.assertEqual(call.args[2], 0o600)
        expected = "".join(json.dumps(obj, ensure_ascii=False, sort_keys=True) + "\n" for obj in objects)
        self.assertEqual(path.read_text(encoding="utf-8"), expected)
        self.assertEqual([json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()], objects)

    def test_jsonl_rejects_raw_newline_before_opening(self):
        with mock.patch.object(fsio.json, "dumps", return_value="broken\nrecord"):
            with self.assertRaisesRegex(ValueError, "raw newline"):
                fsio.append_jsonl(self.path, {})
        self.assertFalse(self.path.exists())

    @unittest.skipIf(os.name == "nt", "POSIX permission bits required")
    def test_jsonl_creation_is_private_and_existing_mode_is_preserved(self):
        fsio.append_jsonl(self.path, {})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        os.chmod(self.path, 0o640)
        fsio.append_jsonl(self.path, {})
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

    def test_jsonl_escapes_all_splitlines_separators(self):
        obj = {"text": "a\u2028b\u2029c\u0085d\x0ce"}
        fsio.append_jsonl(self.path, obj)
        text = self.path.read_text(encoding="utf-8")
        self.assertEqual(len(text.splitlines()), 1)
        for escaped in (r"\u2028", r"\u2029", r"\u0085", r"\f"):
            self.assertIn(escaped, text)
        self.assertEqual(fsio.read_jsonl(self.path), [obj])

    def test_read_jsonl_splits_only_on_newline(self):
        objects = [{"text": "a\u2028b\u2029c\u0085d"}, {"value": 2}]
        # Existing JSONL from other writers may contain raw Unicode separators.
        self.path.write_text("\n".join(json.dumps(obj, ensure_ascii=False) for obj in objects) + "\n", encoding="utf-8")
        self.assertEqual(fsio.read_jsonl(self.path), objects)
        self.path.write_text(json.dumps(objects[0], ensure_ascii=False), encoding="utf-8")
        self.assertEqual(fsio.read_jsonl(self.path), objects[:1])
        self.path.write_text("", encoding="utf-8")
        self.assertEqual(fsio.read_jsonl(self.path), [])

    def test_read_jsonl_reports_invalid_record_without_contents(self):
        self.path.write_text('{}\ninvalid record\n', encoding="utf-8")
        with self.assertRaises(ValueError) as caught:
            fsio.read_jsonl(self.path)
        self.assertIn(str(self.path), str(caught.exception))
        self.assertNotIn("invalid record", str(caught.exception))
        with self.assertRaises(FileNotFoundError):
            fsio.read_jsonl(self.directory / "missing.jsonl")

    def test_raw_newline_is_rejected_under_optimized_python(self):
        script = r"""
import sys
from unittest import mock
sys.path.insert(0, sys.argv[1])
from orcexe import fsio
with mock.patch.object(fsio.json, "dumps", return_value="broken\nrecord"):
    try:
        fsio.append_jsonl(sys.argv[2], {})
    except ValueError:
        sys.exit(0)
sys.exit(1)
"""
        result = subprocess.run([sys.executable, "-B", "-O", "-c", script, str(SOURCE), str(self.path)], capture_output=True)
        self.assertEqual(result.returncode, 0, "Optimized Python did not reject a raw newline")
        self.assertFalse(self.path.exists())

    def test_json_writers_reject_non_finite_numbers_before_touching_target(self):
        for writer in (fsio.atomic_write_json, fsio.append_jsonl):
            for value in (float("nan"), float("inf"), float("-inf")):
                with self.subTest(writer=writer.__name__):
                    self.path.write_text("original", encoding="utf-8")
                    with self.assertRaises(ValueError):
                        writer(self.path, {"number": value})
                    self.assertEqual(self.path.read_text(encoding="utf-8"), "original")
                    self.assertEqual(list(self.directory.iterdir()), [self.path])

    @unittest.skipIf(os.name == "nt", "POSIX permission bits required")
    def test_all_created_parents_have_requested_private_mode(self):
        previous_umask = os.umask(0)
        try:
            for writer, value in ((fsio.atomic_write_text, "text"), (fsio.atomic_write_json, {}), (fsio.append_jsonl, {})):
                for mode in (None, 0o750):
                    root = self.directory / (writer.__name__ + str(mode))
                    path = root / "a" / "b" / "data"
                    kwargs = {} if mode is None else {"dir_mode": mode}
                    writer(path, value, **kwargs)
                    for directory in (root, root / "a", path.parent):
                        self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700 if mode is None else mode)
            self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o700)
        finally:
            os.umask(previous_umask)

    @unittest.skipIf(os.name == "nt", "POSIX permission bits required")
    def test_existing_parent_modes_are_preserved(self):
        os.chmod(self.directory, 0o750)
        fsio.atomic_write_text(self.path, "text")
        fsio.atomic_write_json(self.path, {})
        fsio.append_jsonl(self.path, {})
        self.assertEqual(stat.S_IMODE(self.directory.stat().st_mode), 0o750)

    def test_jsonl_flushes_and_fsyncs_file(self):
        real_fsync = os.fsync
        with mock.patch.object(fsio.os, "fsync", wraps=real_fsync) as synced:
            fsio.append_jsonl(self.path, {"number": 1})
        synced.assert_called_once()
        self.assertEqual(fsio.read_jsonl(self.path), [{"number": 1}])
