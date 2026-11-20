"""Full cross-adapter process matrix; fast contracts retain representative cases."""

import os
import unittest
from tests.contract import test_adapter_contract as contracts


class FullScenarios:
    def test_full_fake_scenarios(self):
        self.check_fake_scenarios(full=True)


def load_tests(loader, standard_tests, pattern):
    if os.environ.get('ORCEXE_SLOW_TESTS') != '1':
        return unittest.TestSuite()
    suite = unittest.TestSuite()
    for name in contracts.ADAPTERS:
        base = getattr(contracts, 'AdapterContract_' + name)
        cls = type('FullScenarios_' + name, (FullScenarios, base), {})
        suite.addTest(cls('test_full_fake_scenarios'))
    return suite
