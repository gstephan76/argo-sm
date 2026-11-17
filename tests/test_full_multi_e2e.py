"""Offline checks for the read-only ambient E2E route assertions."""
import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('multi_e2e', ROOT / 'scripts/full-multi-e2e.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class E2ETests(unittest.TestCase):
    def test_weights_require_one_correct_weighted_rule(self):
        route = {'spec': {'rules': [
            {'backendRefs': [{'name': 'stable'}]},
            {'backendRefs': [{'name': 'stable', 'weight': 50}, {'name': 'canary', 'weight': 50}]},
        ]}}
        mod.check_weights(route, 'stable', 'canary', 50)
        with self.assertRaises(mod.workflow.Failure):
            mod.check_weights(route, 'stable', 'canary', 0)
        route['spec']['rules'].append(route['spec']['rules'][1])
        with self.assertRaises(mod.workflow.Failure):
            mod.check_weights(route, 'stable', 'canary', 50)


if __name__ == '__main__':
    unittest.main()
