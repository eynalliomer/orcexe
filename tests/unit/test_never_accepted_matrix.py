"""T61 / C29: the "never accepted" matrix, every applicable cell built independently with fake CLIs.

Rows are failure kinds, columns are lanes: fast (``orcexe do``; for the budget row a fast-lane
plan task under ``plan --run``, the only entry point with a budget), standard and risky. L1 rows
do not apply to fast; the auditor row applies to risky only. Each cell is its own test (fresh
repository, ledger and fake tools) and asserts the cell never ends ``accepted``: no accepted
ledger state, no chain advance, and (outside standard ``gate_passed``, which keeps its frozen
ref and human land command) no accepted ref or landing command.

The observed matrix and the number of cells exercised are printed when the module ends and,
when ``ORCEXE_MATRIX_OUT`` names a file, written there as JSON.
"""

from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import unittest
from unittest import mock

from tests.unit import test_do
from tests.unit.test_do import DoFixture, EXTEND
from tests.unit.test_risky_lane import RiskyFixture, audit, roles_without
from tests.unit.test_task import APP, TEST, ROOT
from tests.unit.test_task_l1 import rubric
from tests.unit.test_planning import task as plan_task
from orcexe import do, planrun, route


ROWS = ('empty_diff', 'quota', 'cap_reached', 'timeout', 'pipes_held', 'model_mismatch',
        'l1_invalid', 'l1_unavailable', 'no_auditor', 'budget')
LANES = ('fast', 'standard', 'risky')
NOT_APPLICABLE = {('fast', 'l1_invalid'), ('fast', 'l1_unavailable'), ('fast', 'no_auditor'),
                  ('standard', 'no_auditor')}
EXPECTED_CELLS = len(ROWS) * len(LANES) - len(NOT_APPLICABLE)
OBSERVED = {}
FAKE = ROOT / 'tests/fixtures/fake-cli'


def record(lane, row, state, reason):
    OBSERVED[(lane, row)] = (state, reason)


def render():
    width = max(len(r) for r in ROWS)
    lines = ['C29 never-accepted matrix (state / reason):',
             '| ' + 'row'.ljust(width) + ' | ' + ' | '.join(LANES) + ' |',
             '| ' + '-' * width + ' |' + '|'.join(' --- ' for _ in LANES) + '|']
    for row in ROWS:
        cells = []
        for lane in LANES:
            if (lane, row) in NOT_APPLICABLE:
                cells.append('N/A')
            elif (lane, row) in OBSERVED:
                cells.append('{} / {}'.format(*OBSERVED[(lane, row)]))
            else:
                cells.append('not run')
        lines.append('| ' + row.ljust(width) + ' | ' + ' | '.join(cells) + ' |')
    lines.append('cells exercised: {} of {} applicable'.format(len(OBSERVED), EXPECTED_CELLS))
    return '\n'.join(lines)


def tearDownModule():
    print('\n' + render(), flush=True)
    target = os.environ.get('ORCEXE_MATRIX_OUT')
    if target:
        Path(target).write_text(json.dumps(dict(
            rows=list(ROWS), lanes=list(LANES), not_applicable=sorted('/'.join(c) for c in NOT_APPLICABLE),
            observed={lane + '/' + row: list(value) for (lane, row), value in sorted(OBSERVED.items())},
            cells=len(OBSERVED), expected=EXPECTED_CELLS), indent=2) + '\n')


def wrapper(path, fake, token, edit, scenario=None):
    """A fake CLI that, on its model call only, makes ``edit`` (path -> text appended) in its cwd
    and then runs the shipped fake with ``scenario``; probes pass through unchanged."""
    path.write_text('#!' + sys.executable + '\n'
        'import os, runpy, sys\nfrom pathlib import Path\n'
        'if ' + repr(token) + ' in sys.argv:\n'
        + ('    os.environ["ORCEXE_FAKE_SCENARIO"] = ' + repr(scenario) + '\n' if scenario else '')
        + '    for name, text in ' + repr(sorted(edit.items())) + ':\n'
        '        with open(Path.cwd() / name, "a", encoding="utf-8") as handle:\n'
        '            handle.write(text)\n'
        'sys.path.insert(0, ' + repr(str(fake.parent)) + ')\n'
        'runpy.run_path(' + repr(str(fake)) + ', run_name="__main__")\n')
    path.chmod(0o700)
    return str(path)


class FastCells(DoFixture):
    """`orcexe do`: one attempt, gate_lite only."""

    def assert_never_accepted(self, row, result):
        record('fast', row, result['verdict'],
               result.get('detail') if result.get('failed_check') == 'run_status' else result.get('failed_check'))
        self.assertNotEqual(result['verdict'], 'accepted', result)
        self.assertNotEqual(result['exit_code'], 0, result)
        self.assertFalse(result.get('land_commands'), result)
        self.assertEqual(self.git('for-each-ref', 'refs/orcexe/accepted/'), '')
        self.assertNotIn('accepted', [r['data']['to'] for r in self.log().records(event='task.state')])

    def codex(self, scenario):
        self.env['ORCEXE_BIN_CODEX'] = wrapper(self.root / 'codex-edit', FAKE / 'codex', 'exec',
                                               {'src/app.py': EXTEND}, scenario)

    def test_control_fixture_can_accept(self):
        result = self.invoke()
        self.assertEqual(result['verdict'], self.accepted_verdict, result)

    def test_empty_diff(self):
        self.env.pop('ORCEXE_FAKE_EDIT_FILE')
        self.env.pop('ORCEXE_FAKE_EDIT_TEXT')
        result = self.invoke()
        self.assertEqual((result['failed_check'], result['detail']), ('run_status', 'empty_diff'), result)
        self.assert_never_accepted('empty_diff', result)

    def test_quota(self):
        self.codex('quota')
        gather = route.gather_access

        def exhaust_after_access(adapters):
            access = gather(adapters)
            self.env['ORCEXE_FAKE_APPSERVER_SCENARIO'] = 'quota-exhausted'
            adapters[0]._env['ORCEXE_FAKE_APPSERVER_SCENARIO'] = 'quota-exhausted'
            return access
        with mock.patch.object(do.route, 'gather_access', side_effect=exhaust_after_access):
            result = self.invoke()
        self.assertEqual((result['run_status'], result['failed_check']), ('quota', 'run_status'), result)
        self.assert_never_accepted('quota', result)

    def test_cap_reached(self):
        # The hole T61 closes: a 5 MiB line was a clean `completed` run with a real edit.
        self.codex('big-line')
        result = self.invoke()
        self.assertEqual((result['run_status'], result['failed_check'], result['detail']),
                         ('completed', 'run_status', 'output_capped'), result)
        self.assertEqual(result['changed_files'], [])  # the gate stopped before collecting the diff
        ended = [r['data'] for r in self.log().records(event='attempt.ended')]
        self.assertIn('output_capped', ended[0]['warnings'])
        self.assert_never_accepted('cap_reached', result)

    def test_timeout(self):
        self.codex('stall')
        self.env['ORCEXE_FAKE_SLEEP'] = '30'
        result = self.invoke(timeout=.4)
        self.assertIn(result['run_status'], ('timeout', 'stalled'), result)
        self.assert_never_accepted('timeout', result)

    def test_pipes_held(self):
        if os.name == 'nt':
            self.skipTest('pipes_held is POSIX-only (background-child fixture)')
        self.codex('background-child')
        result = self.invoke()
        self.assertEqual(result['failed_check'], 'pipes_held', result)
        self.assert_never_accepted('pipes_held', result)

    def test_model_mismatch(self):
        # The hole T61 closes: Claude served another model than the routed one, with a real edit.
        self.env['ORCEXE_FAKE_SCENARIO'] = 'auth-missing'  # Codex unavailable, so `do` routes to Claude
        self.claude_scenario = 'model-mismatch'
        self.env['ORCEXE_BIN_CLAUDE_CODE'] = wrapper(self.root / 'claude-edit', FAKE / 'claude', '-p',
                                                     {'src/app.py': EXTEND})
        result = self.invoke()
        self.assertEqual(result['route']['tool'], 'claude-code', result)
        self.assertEqual((result['run_status'], result['failed_check'], result['detail']),
                         ('completed', 'run_status', 'model_mismatch'), result)
        self.assert_never_accepted('model_mismatch', result)


class PlanLaneBase(RiskyFixture):
    """`task` / `plan --run` helpers for the standard and risky lanes (and the fast budget cell)."""
    lane = None
    CODEX = dict(tool='codex', model='gpt-6.1-sol', effort='high')

    def setUp(self):
        super().setUp()
        self.use_plan()

    def use_plan(self, route_=None, lane=None):
        lane = lane or self.lane
        options = dict(goal='Return two and update its test', lane=lane, files={'allow': ['src/*.py', 'tests/**']},
                       done=['app returns 2 and its test passes'], route=route_ or dict(self.CODEX))
        if lane == 'risky':
            options['lane_basis'] = ['manager:risky', 'cli:classify:risky']
        if lane == 'fast':
            options = dict(route=route_ or dict(self.CODEX))
        self.saved = self.save([plan_task(**options)])
        self.plan_id = self.saved['plan_id']

    def codex_env(self, **values):
        wrapper_path = self.directory / 'fake-codex'
        source = wrapper_path.read_text()
        old = 'os.environ.update('
        wrapper_path.write_text(source.replace(old, 'os.environ.update(' + repr(values) + ')\n' + old, 1))

    def ready(self):
        if self.lane == 'risky':
            self.propose_and_approve()

    def run_cell(self, *extra, roles=None):
        self.ready()
        patch = (mock.patch.object(route, 'load_roles', side_effect=lambda path=None: roles_without(**roles))
                 if roles is not None else nullcontext())
        with patch:
            code, result, error = self.dispatch(*extra)
        return result, error

    def assert_never_accepted(self, row, state, reason, lane=None):
        lane = lane or self.lane
        record(lane, row, state, reason)
        self.assertNotEqual(state, 'accepted')
        self.assertNotIn('accepted', [r['data']['to'] for r in self.log().records(event='task.state')])
        self.assertIn(planrun.chain_tip(self.main, self.saved), (None, self.head))  # no chain advance
        if not (lane == 'standard' and state == 'gate_passed'):
            self.assertEqual(self.accepted_refs(), '')

    def run_status_failures(self):
        return [r['data']['reason'] for r in self.log().records(event='gate.failed')]

    def warnings(self):
        return [w for r in self.log().records(event='attempt.ended') for w in r['data']['warnings']]

    def check(self, row, result, error):
        self.assert_never_accepted(row, result['state'], result['reason'])
        if self.lane == 'risky':
            self.assertIsNone(result['land'], (result, error))

    def plan_run(self, *extra):
        code, out, err = self.run_cli('plan', '--run', self.plan_id, '--repo', str(self.main), '--json', *extra)
        return code, json.loads(out), err


class PlanLaneCells:
    """The standard/risky cells (mixed into a PlanLaneBase subclass per lane)."""

    # ---- executor rows

    def test_empty_diff(self):
        self.actions.write_text(json.dumps([{}]))
        result, error = self.run_cell()
        self.assertEqual(self.run_status_failures()[0], 'run_status', (result, error))
        self.check('empty_diff', result, error)

    def test_quota(self):
        self.executor_scenarios(['quota'])
        self.codex_env(ORCEXE_FAKE_APPSERVER_SCENARIO='quota-exhausted')
        result, error = self.run_cell()
        statuses = [r['data']['status'] for r in self.log().records(event='attempt.ended')]
        self.assertEqual(set(statuses), {'quota'}, (result, error))
        self.assertEqual((result['state'], result['attempts']), ('exhausted', 3), (result, error))
        self.check('quota', result, error)

    def test_cap_reached(self):
        # The hole T61 closes: a real edit plus a 5 MiB line passed every gate on every lane.
        self.executor_scenarios(['big-line'])
        self.evaluator([rubric(), audit()])
        result, error = self.run_cell()
        self.assertIn('output_capped', self.warnings(), (result, error))
        self.assertEqual(set(self.run_status_failures()), {'run_status'}, (result, error))
        self.assertNotIn('l1.started', self.events())
        self.check('cap_reached', result, error)

    def test_timeout(self):
        self.executor_scenarios(['stall'])
        self.codex_env(ORCEXE_FAKE_SLEEP='30')
        result, error = self.run_cell('--timeout-min', '0.02')
        statuses = {r['data']['status'] for r in self.log().records(event='attempt.ended')}
        self.assertTrue(statuses <= {'timeout', 'stalled'} and statuses, (statuses, result, error))
        self.check('timeout', result, error)

    def test_pipes_held(self):
        if os.name == 'nt':
            self.skipTest('pipes_held is POSIX-only (background-child fixture)')
        self.executor_scenarios(['background-child'])
        result, error = self.run_cell()
        self.assertEqual((result['state'], result['reason']), ('rejected', 'pipes_held'), (result, error))
        self.check('pipes_held', result, error)

    def test_model_mismatch(self):
        # The hole T61 closes: the routed Claude model asked, another one served, real edits made.
        self.use_plan(dict(tool='claude-code', model='claude-sonnet-5-5', effort='high'))
        edits = {'src/app.py': '\nTWO = 2\n', 'tests/test_app.py': '\n# checked\n'}
        self.env['ORCEXE_BIN_CLAUDE_CODE'] = wrapper(self.directory / 'claude-edit', FAKE / 'claude', '-p',
                                                     edits, 'model-mismatch')
        result, error = self.run_cell()
        ended = [r['data'] for r in self.log().records(event='attempt.ended')]
        self.assertTrue(ended, (result, error))
        self.assertTrue(all(e['model_actual'] == 'claude-haiku-5-5' and 'model_mismatch' in e['warnings']
                            and e['status'] == 'completed' for e in ended), ended)
        self.assertEqual(set(self.run_status_failures()), {'run_status'}, (result, error))
        self.check('model_mismatch', result, error)

    # ---- L1 / auditor rows

    def test_l1_invalid(self):
        self.evaluator(['not a rubric at all'])
        result, error = self.run_cell()
        self.assertEqual((result['state'], result['reason']), ('gate_passed', 'l1_invalid'), (result, error))
        self.check('l1_invalid', result, error)

    def test_l1_unavailable(self):
        # No Claude binary: no cross-family evaluator can run.
        result, error = self.run_cell()
        self.assertEqual((result['state'], result['reason']), ('gate_passed', 'l1_unavailable'), (result, error))
        self.check('l1_unavailable', result, error)

    # ---- budget row (plan --run)

    def test_budget(self):
        calls = {'standard': '1', 'risky': '2'}[self.lane]
        self.evaluator([rubric(), audit()])
        self.ready()
        code, result, error = self.plan_run('--max-calls', calls)
        item = result['tasks'][0]
        self.assertEqual((code, result['state'], result['stop_reason'], result['calls']),
                         (1, 'partial', 'budget', int(calls)), (result, error))
        self.assertEqual((item['state'], item['reason']), ('interrupted', 'budget'), result)
        # Executor (and on risky L1) ran and passed; the next call (L1 / auditor) never started.
        self.assertEqual(self.events().count('l1.started'), {'standard': 0, 'risky': 1}[self.lane])
        self.assertNotIn('audit.started', self.events())
        self.assertIsNone(result['land'])
        self.assert_never_accepted('budget', item['state'], item['reason'])


class StandardCells(PlanLaneCells, PlanLaneBase):
    lane = 'standard'

    def test_control_fixture_can_accept(self):
        self.evaluator([rubric()])
        result, error = self.run_cell()
        self.assertEqual(result['state'], self.accepted_state, (result, error))


class RiskyCells(PlanLaneCells, PlanLaneBase):
    lane = 'risky'

    def test_control_fixture_can_accept(self):
        self.evaluator([rubric(), audit()])
        result, error = self.run_cell()
        self.assertEqual(result['state'], self.accepted_state, (result, error))

    def test_no_auditor(self):
        self.evaluator([rubric()])
        result, error = self.run_cell(roles=dict(stage='auditor'))
        self.assertEqual((result['state'], result['reason']), ('gate_passed', 'auditor_unavailable'), (result, error))
        self.assertNotIn('audit.started', self.events())
        self.check('no_auditor', result, error)


class FastBudgetCell(PlanLaneBase):
    """Fast column, budget row: `orcexe do` has no budget, so a fast-lane task under `plan --run`."""
    lane = 'fast'

    def test_budget(self):
        self.actions.write_text(json.dumps([{}, {'README.md': 'Example corrected\n'}]))
        code, result, error = self.plan_run('--max-calls', '1')
        item = result['tasks'][0]
        self.assertEqual((code, result['state'], result['stop_reason'], result['calls']),
                         (1, 'partial', 'budget', 1), (result, error))
        self.assertEqual((item['state'], item['reason'], item['attempts']), ('interrupted', 'budget', 1), result)
        self.assertEqual(json.loads(self.counter.read_text()), 1)  # attempt 2 never started
        self.assert_never_accepted('budget', item['state'], item['reason'])


if __name__ == '__main__':
    unittest.main()
