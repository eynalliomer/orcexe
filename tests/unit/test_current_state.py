"""Current-state regeneration preserves all human blocks without proposals."""

import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from orcexe import current_state, published


OPEN = '<!-- orcexe:human -->'
CLOSE = '<!-- /orcexe:human -->'


class CurrentStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.main = Path(self.tmp.name)
        self.directory = self.main / '.orcexe'
        self.path = self.directory / 'current-state.md'
        self.head = mock.patch('orcexe.gitutil.head', return_value='a' * 40)
        self.head.start()
        self.addCleanup(self.head.stop)

    def write(self, unknowns=0):
        return current_state.write(self.main, project={'unknowns': [{}] * unknowns}, plan={})

    def assert_stable(self):
        first = self.path.read_bytes()
        self.assertEqual(self.write(1), [])
        self.assertEqual(self.path.read_bytes(), first)
        self.assertEqual(first.count(b'# Current state'), 1)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.directory.glob('current-state.proposed*')))

    def test_zero_one_two_three_blocks_anywhere_and_added_second(self):
        for count in range(4):
            with self.subTest(count=count):
                self.write()
                generated = current_state.render(self.main, project={}, plan={})
                outside = published.HUMAN.sub('', generated)
                blocks = [OPEN + '\r\nExact note {}.  \r\n'.format(i) + CLOSE for i in range(count)]
                # Include blocks before, inside, and after the generated section.
                middle = outside.index('Unknowns:')
                positions = [0, middle, len(outside)]
                old = outside
                for position, block in reversed(list(zip(positions, blocks))):
                    old = old[:position] + block + old[position:]
                self.path.write_bytes(old.encode('utf-8'))
                self.assertEqual(self.write(1), [])
                result = published.text(self.path)
                self.assertIn('Unknowns: 1', result)
                expected = blocks or [OPEN + '\n' + CLOSE]
                self.assertEqual(published.HUMAN.findall(result), expected)
                self.assertGreater(result.index(OPEN), result.index('Land:'))
                self.assert_stable()
                self.assertFalse((self.directory / 'current-state.previous.md').exists())
        # Real-run failure: append a second block after the empty placeholder.
        self.path.unlink()
        self.write()
        self.path.write_bytes((published.text(self.path) + '\n' + OPEN + '\nAdded\n' + CLOSE).encode())
        self.assertEqual(self.write(1), [])
        self.assertEqual(len(published.HUMAN.findall(published.text(self.path))), 2)
        self.assert_stable()

    def test_unclosed_and_nested_keep_everything_from_first_opening(self):
        tails = [OPEN + '\r\nUnclosed notes\r\n',
                 OPEN + '\nOuter\n' + OPEN + '\nInner\n' + CLOSE + '\nTrailing text',
                 OPEN + '\nGood\n' + CLOSE + '\n' + OPEN + '\nUnclosed\n']
        for tail in tails:
            with self.subTest(tail=tail):
                self.write()
                generated = current_state.render(self.main, project={}, plan={})
                old = published.HUMAN.sub('', generated).rstrip() + '\n\n' + tail
                self.path.write_bytes(old.encode())
                self.assertEqual(self.write(1), [])
                self.assertTrue(published.text(self.path).endswith(tail))
                self.assert_stable()

    def test_outside_edit_saved_once_and_reported_then_summary_regenerated(self):
        self.write()
        old = published.text(self.path).replace('HEAD: ', 'Human HEAD: ')
        self.path.write_bytes(old.encode())
        messages = io.StringIO()
        with contextlib.redirect_stderr(messages):
            published.report(self.write(1))
        previous = self.directory / 'current-state.previous.md'
        self.assertEqual(previous.read_bytes(), old.encode())
        self.assertEqual(previous.stat().st_mode & 0o777, 0o600)
        self.assertIn(str(previous), messages.getvalue())
        self.assertNotIn('human_edit_kept', messages.getvalue())
        self.assertNotIn('Human HEAD:', published.text(self.path))
        self.assertIn('Unknowns: 1', published.text(self.path))
        self.assert_stable()
        self.path.write_bytes((published.text(self.path) + '\nAnother outside edit').encode())
        self.write(1)
        self.assertEqual(previous.read_bytes(), old.encode())
        self.assert_stable()

    def test_without_receipt_save_existing_outside_text(self):
        self.directory.mkdir()
        self.path.write_bytes(b'Unrecorded human text\n')
        self.write(1)
        self.assertEqual((self.directory / 'current-state.previous.md').read_bytes(), b'Unrecorded human text\n')
        self.assert_stable()

    def test_foreign_summary_and_backup_symlinks_refused(self):
        target = self.main / 'private'
        target.write_text('untouched')
        self.directory.mkdir()
        self.path.symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'output_symlink'):
            self.write()
        self.path.unlink()
        self.write()
        self.path.write_bytes((published.text(self.path) + '\nOutside edit').encode())
        (self.directory / 'current-state.previous.md').symlink_to(target)
        with self.assertRaisesRegex(ValueError, 'output_symlink'):
            self.write()
        self.assertEqual(target.read_text(), 'untouched')
