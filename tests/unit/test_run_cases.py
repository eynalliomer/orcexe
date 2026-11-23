"""T62 / C35: the case-set runner (tests/tools/run_cases.py).

Pure tests pin the case format and the mapping of ledger records to the catching layer
(gate / l0 / l1 / audit) and the model calls started before the catch. End-to-end tests run
the public examples, plus temporary L1, risky-auditor and control cases, through the real
`orcexe task` path with fake CLIs (everything in temporary directories).
"""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

from orcexe import isolation
from tests.unit.test_task import ROOT


SPEC = importlib.util.spec_from_file_location('run_cases', str(ROOT / 'tests/tools/run_cases.py'))
run_cases = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_cases)
EXAMPLES = ROOT / 'tests/cases/examples'


def rec(event, **data):
    return dict(event=event, task='T1', data=data)


def example(name):
    return json.loads((EXAMPLES / name / 'case.json').read_text())


class MappingTests(unittest.TestCase):
    def mapped(self, *records):
        result = run_cases.classify_records(list(records))
        return (result['caught_by'], result['check'], run_cases.reviewer_calls(result['calls_before_catch']),
                (result['calls_before_catch'] or {}).get('executor'))

    def test_gate_run_status_and_scope(self):
        self.assertEqual(self.mapped(rec('attempt.started'), rec('attempt.ended'),
                                     rec('gate.failed', reason='run_status'), rec('attempt.started'),
                                     rec('gate.failed', reason='diff_collect')),
                         ('gate', 'run_status', 0, 1))
        self.assertEqual(self.mapped(rec('attempt.started'), rec('gate.failed', reason='outside_allow'),
                                     rec('lane.escalated'), rec('task.state', to='rejected')),
                         ('gate', 'outside_allow', 0, 1))
        for reason in ('test_floor', 'secrets', 'risk_path_touched', 'pipes_held', 'git_controls_changed'):
            self.assertEqual(self.mapped(rec('attempt.started'), rec('gate.failed', reason=reason))[:2],
                             ('gate', reason))

    def test_l0_is_named_by_its_first_failed_check_not_the_gate_record(self):
        self.assertEqual(self.mapped(rec('attempt.started'), rec('l0.failed', failed_checks=['skip_added', 'tests_missing']),
                                     rec('gate.failed', reason='skip_added')),
                         ('l0', 'skip_added', 0, 1))
        # A gate.failed naming an L0 check id without its l0 record still maps to l0.
        self.assertEqual(self.mapped(rec('attempt.started'), rec('gate.failed', reason='assertion_missing'))[:2],
                         ('l0', 'assertion_missing'))

    def test_l1_counts_the_catching_call(self):
        self.assertEqual(self.mapped(rec('attempt.started'), rec('l0.passed'), rec('l1.started'),
                                     rec('l1.verdict', criteria=['d1:pass', 'd2:fail']), rec('gate.failed', reason='d2'),
                                     rec('attempt.started'), rec('l1.started')),
                         ('l1', 'd2', 1, 1))
        # An unavailable candidate followed by a failing one: both calls precede the catch.
        self.assertEqual(self.mapped(rec('attempt.started'), rec('l1.started'), rec('l1.verdict', criteria=[], reason='l1_unavailable'),
                                     rec('l1.started'), rec('l1.verdict', criteria=['d1:fail'])),
                         ('l1', 'd1', 2, 1))

    def test_audit_fail_and_auditor_wrote(self):
        self.assertEqual(self.mapped(rec('attempt.started'), rec('l1.started'), rec('l1.verdict', criteria=['d1:pass']),
                                     rec('audit.started'), rec('audit.verdict', verdict='fail', findings=['a1|P1|src/app.py']),
                                     rec('gate.failed', reason='audit')),
                         ('audit', 'a1|P1|src/app.py', 2, 1))
        self.assertEqual(self.mapped(rec('attempt.started'), rec('l1.started'), rec('l1.verdict', criteria=['d1:pass']),
                                     rec('audit.started'), rec('audit.verdict', verdict=None, reason='auditor_wrote'),
                                     rec('gate.failed', reason='auditor_wrote'))[:3],
                         ('audit', 'auditor_wrote', 2))

    def test_no_catch_for_accepted_or_unavailable_reviewers(self):
        for records in ([rec('attempt.started'), rec('l1.started'), rec('l1.verdict', criteria=['d1:pass']),
                         rec('gate.passed'), rec('task.state', to='accepted')],
                        [rec('attempt.started'), rec('l1.started'), rec('l1.verdict', criteria=[], reason='l1_invalid'),
                         rec('gate.passed'), rec('task.state', to='gate_passed')]):
            result = run_cases.classify_records(records)
            self.assertEqual((result['caught_by'], result['calls_before_catch']), (None, None))
            self.assertEqual(result['calls'], dict(executor=1, l1=1, auditor=0))

    def test_compare(self):
        case = dict(expect=dict(accepted=False, caught_by='gate', max_model_calls_before_catch=0))
        good = dict(error=None, accepted=False, land=False, accepted_ref=False, caught_by='gate',
                    model_calls_before_catch=0, state='exhausted')
        self.assertEqual(run_cases.compare(case, good), [])
        self.assertEqual(len(run_cases.compare(case, dict(good, caught_by='l0'))), 1)
        self.assertEqual(len(run_cases.compare(case, dict(good, model_calls_before_catch=1))), 1)
        self.assertEqual(len(run_cases.compare(case, dict(good, accepted=True, land=True, caught_by=None,
                                                          model_calls_before_catch=None))), 3)
        self.assertEqual(run_cases.compare(case, dict(error='boom')), ['runner error: boom'])


class CaseFormatTests(unittest.TestCase):
    def load(self, value):
        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / 'case.json').write_text(json.dumps(value))
            return run_cases.load_case(raw)

    def test_examples_load_with_safe_defaults(self):
        names = sorted(p.parent.name for p in EXAMPLES.glob('*/case.json'))
        self.assertEqual(names, ['empty-done', 'skip-test', 'wrong-file'])
        case = run_cases.load_case(EXAMPLES / 'empty-done')
        self.assertEqual(case['rubric'], [dict(id='d1', **{'pass': True}, reason='observed behavior')])
        self.assertEqual(case['auditor'], dict(schema='orcexe.audit.v1', verdict='pass', findings=[]))
        self.assertEqual(case['executor']['writes'], [])

    def test_invalid_cases_are_rejected(self):
        base = example('wrong-file')
        changes = [
            lambda v: v.update(schema='orcexe.case.v0'),
            lambda v: v.update(lane='fast'),
            lambda v: v['repo']['files'].update({'../escape.py': 'x'}),
            lambda v: v['executor']['writes'].append({'path': '/etc/hosts', 'content': 'x'}),
            lambda v: v['executor']['writes'].append({'path': 'a.py', 'delete': True, 'content': 'x'}),
            lambda v: v['task'].update(allow=[]),
            lambda v: v['expect'].update(accepted=True),
            lambda v: v['expect'].update(caught_by='l2'),
            lambda v: v['expect'].update(max_model_calls_before_catch=-1),
            lambda v: v.update(evaluator={'rubric': 'maybe'}),
        ]
        for index, change in enumerate(changes):
            value = copy.deepcopy(base)
            change(value)
            with self.subTest(index=index), self.assertRaises(run_cases.CaseError):
                self.load(value)


class EndToEndTests(unittest.TestCase):
    def test_public_examples_match_and_none_is_accepted(self):
        report = run_cases.run_all(EXAMPLES)
        self.assertEqual(report['summary'], dict(total=3, accepted=0, mismatches=0), run_cases.render(report))
        observed = {c['id']: (c['caught_by'], c['check'], c['model_calls_before_catch'], c['land'], c['accepted_ref'])
                    for c in report['cases']}
        self.assertEqual(observed, {
            'empty-done': ('gate', 'run_status', 0, False, False),
            'skip-test': ('l0', 'skip_added', 0, False, False),
            'wrong-file': ('gate', 'outside_allow', 0, False, False)})
        for case in report['cases']:
            self.assertEqual(case['calls_before_catch'], dict(executor=1, l1=0, auditor=0))
            self.assertEqual(case['calls']['l1'] + case['calls']['auditor'], 0)

    def test_l1_and_audit_catches_and_an_accepted_control_is_a_mismatch(self):
        base = example('wrong-file')
        clean = base['executor']['writes'][:2]
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            def put(identifier, **changes):
                value = copy.deepcopy(base)
                value.update(id=identifier, title=identifier,
                             executor=dict(scenario='success', writes=clean, final_text='DONE'), **changes)
                (root / identifier).mkdir()
                (root / identifier / 'case.json').write_text(json.dumps(value))
            put('l1-fail', evaluator={'rubric': [{'id': 'd1', 'pass': False, 'reason': 'edge case missing'}]},
                expect=dict(accepted=False, caught_by='l1', max_model_calls_before_catch=1))
            put('risky-audit', lane='risky', auditor=dict(schema='orcexe.audit.v1', verdict='fail', findings=[
                dict(id='a1', severity='P1', path='src/app.py', reason='Return value not validated.')]),
                expect=dict(accepted=False, caught_by='audit', max_model_calls_before_catch=2))
            put('control', expect=dict(accepted=False, caught_by='gate', max_model_calls_before_catch=0))
            report = run_cases.run_all(root)
            self.assertEqual(run_cases.main([str(root / 'l1-fail')]), 0)
        cases = {c['id']: c for c in report['cases']}
        text = run_cases.render(report)
        self.assertEqual((cases['l1-fail']['caught_by'], cases['l1-fail']['check'],
                          cases['l1-fail']['model_calls_before_catch']), ('l1', 'd1', 1), text)
        self.assertEqual((cases['risky-audit']['caught_by'], cases['risky-audit']['check'],
                          cases['risky-audit']['model_calls_before_catch'],
                          cases['risky-audit']['calls_before_catch']),
                         ('audit', 'a1|P1|src/app.py', 2, dict(executor=1, l1=1, auditor=1)), text)
        for name in ('l1-fail', 'risky-audit'):
            self.assertTrue(cases[name]['match'], text)
            self.assertFalse(cases[name]['land'] or cases[name]['accepted_ref'], text)
        control = cases['control']
        self.assertFalse(control['match'], text)
        self.assertIsNone(control['caught_by'], text)
        if isolation.backend() != 'none':
            self.assertEqual((control['state'], control['accepted'], control['land']), ('accepted', True, True), text)
        self.assertIn('MISMATCH control: caught_by None != expected gate', text)
        self.assertEqual(report['summary']['mismatches'], 1)


if __name__ == '__main__':
    unittest.main()
