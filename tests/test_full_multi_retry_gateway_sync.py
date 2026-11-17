import copy
import importlib.util
from pathlib import Path
import unittest

p = Path(__file__).resolve().parent.parent / 'scripts/full-multi-retry-gateway-sync.py'
s = importlib.util.spec_from_file_location('gateway_retry', p)
m = importlib.util.module_from_spec(s)
s.loader.exec_module(m)


class GatewayRetryTests(unittest.TestCase):
    def test_only_recognized_automated_revision_is_eligible(self):
        errors = [{'kind': 'Gateway', 'name': name, 'status': 'SyncFailed',
                   'message': 'gateways is forbidden: cannot create resource "gateways"'}
                  for name in ('full-multi-gateway', 'full-multi-waypoint')]
        app = {'spec': {'source': {'repoURL': 'https://github.com/gstephan76/argo-sm.git', 'path': 'full-multi',
                                   'targetRevision': 'multi-ambient'}, 'destination': {'namespace': 'multi-ambient'}},
               'operation': {'initiatedBy': {'automated': True}, 'sync': {'revision': 'abc'}},
               'status': {'operationState': {'phase': 'Running', 'syncResult': {'revision': 'abc', 'resources': errors}}}}
        self.assertEqual(m.validate(app, 'abc'), 'terminate')
        with self.assertRaises(m.Failure):
            m.validate(app, 'wrong')
        bad = copy.deepcopy(app)
        bad['status']['operationState']['syncResult']['resources'][0]['message'] = 'other failure'
        with self.assertRaises(m.Failure):
            m.validate(bad, 'abc')
        app['status']['operationState']['phase'] = 'Terminating'
        self.assertEqual(m.validate(app, 'abc'), 'waiting')
        app['operation'] = None
        app['status']['operationState']['phase'] = 'Failed'
        app['status']['operationState']['message'] = 'Operation terminated'
        self.assertEqual(m.validate(app, 'abc'), 'resync')


if __name__ == '__main__':
    unittest.main()
