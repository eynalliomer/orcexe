"""Additive task event schemas for the existing, read-only ledger engine.

Register at package import so all ledger readers (including later CLI processes)
understand task records. Existing required fields and validation stay intact.
"""


class _OptionalShape(dict):
    """The ledger iterates required keys and looks up all supplied fields."""

    def __init__(self, required, **optional):
        super().__init__(required, **optional)
        self.required = tuple(required)

    def __iter__(self):
        return iter(self.required)

    def keys(self):
        return set(self.required)


def register(ledger):
    ledger._ENUMS['isolation'] = frozenset(('seatbelt', 'bwrap', 'none'))
    # K17: why this evaluator may judge the executor's work (risky lanes refuse same_family).
    ledger._ENUMS['l1_basis'] = frozenset(('cross_family', 'cursor_read_only', 'same_family'))
    ledger._SHAPES['gate.result'] = _OptionalShape(ledger._SHAPES['gate.result'], isolation='isolation')
    ledger._SHAPES.update({
        'contract.frozen': dict(plan_id='text', contract_sha256='sha256', path='text', head='text'),
        'attempt.started': dict(plan_id='text', run_id='text', attempt='positive',
                                contract_sha256='sha256', tool='text', model='text', effort='text'),
        'gate.passed': _OptionalShape(dict(plan_id='text', run_id='text', attempt='positive', reason='text?'), isolation='isolation'),
        'gate.failed': _OptionalShape(dict(plan_id='text', run_id='text', attempt='positive', reason='text?'), isolation='isolation'),
        'l0.passed': dict(plan_id='text', run_id='text', attempt='positive', checks='list', failed_checks='list'),
        'l0.failed': dict(plan_id='text', run_id='text', attempt='positive', checks='list', failed_checks='list'),
        # Optional T51 fields keep older ledgers readable: basis (K17), the evaluated
        # tree and output folder number (P2-3 memory), and the run whose verdict was reused.
        'l1.started': _OptionalShape(dict(plan_id='text', run_id='text', attempt='positive', tool='text', model='text'),
                                     basis='l1_basis', call='positive'),
        'l1.verdict': _OptionalShape(dict(plan_id='text', run_id='text', attempt='positive', model='text',
                                          reason='text?', criteria='list'),
                                     basis='l1_basis', tree='text', call='positive', reused_from='text'),
        'task.state': _OptionalShape(ledger._SHAPES['task.state'], plan_id='text', commit='text'),
        'lane.escalated': _OptionalShape(ledger._SHAPES['lane.escalated'],
                                         suggested_lane='lane', paths='list', plan_id='text'),
    })
