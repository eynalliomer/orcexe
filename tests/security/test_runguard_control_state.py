"""An executor must not change CLI-owned control state during a run.

Use the real task entry point and shipped fake executor. Sandbox evidence is
simulated as in test_planrun; the assertion tests the observer on every host.
"""

import json
from unittest import mock

from tests.unit import test_task
from tests.unit.test_planning import task as plan_task
from orcexe import isolation


class ExecutorControlStateTests(test_task.TaskFixture):
    def test_executor_overwriting_the_active_lock_is_state_tampered(self):
        self.saved = self.save([plan_task()])
        self.actions.write_text(json.dumps([{'README.md': 'Corrected spelling\n'}]))
        lock = self.main / '.orcexe/do.lock'
        observed = []

        def overwrite_lock(request):
            self.assertTrue(lock.is_file(), 'the CLI must hold its lock at dispatch')
            observed.append(lock.read_bytes())
            # The fake model executes this write inside adapter.run, while the
            # runguard is observing. Preserve valid JSON to avoid a parse-error
            # refusal masking the absence of foreign-write detection.
            value = json.loads(lock.read_text())
            value['task_id'] = 'foreign-executor'
            lock.write_text(json.dumps(value) + '\n')

        with mock.patch.object(isolation, 'evidence', return_value='seatbelt'):
            code, result, error = self.invoke(before=overwrite_lock)

        self.assertEqual(len(observed), 1, 'the fake executor must actually run')
        self.assertEqual((code, result['state'], result['reason']),
                         (1, 'rejected', 'state_tampered'), (result, error))
        self.assertIsNone(result['land'])
