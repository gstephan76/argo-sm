"""Offline regression tests: no oc access and no cluster mutation."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

PATH = Path(__file__).resolve().parents[1] / 'scripts' / 'full-multi-bootstrap.py'
spec = importlib.util.spec_from_file_location('full_multi_bootstrap', PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class BootstrapTests(unittest.TestCase):
    def test_no_controller_is_not_healthy(self):
        self.assertFalse(mod.manager_ready({'spec': {'namespaceScoped': False},
                                           'status': {'phase': 'Pending', 'rolloutController': 'Pending'}}))
        self.assertFalse(mod.manager_ready({'spec': {'namespaceScoped': True},
                                           'status': {'phase': 'Available', 'rolloutController': 'Available'}}))
        self.assertTrue(mod.manager_ready({'spec': {'namespaceScoped': False},
                                          'status': {'phase': 'Available', 'rolloutController': 'Available'}}))

    def test_missing_secrets_or_keys_fail(self):
        self.assertEqual(len(mod.validate_secrets({})), 2)
        good = {'data': {'password': 'not-a-real-secret', 'root-password': 'dummy'}}
        self.assertFalse(mod.validate_secrets({'a': good, 'b': good}))
        self.assertIn('root-password', mod.validate_secrets({'a': {'data': {'password': 'x'}}, 'b': good})[0])

    def test_sync_requires_exact_commit_and_synced(self):
        s = mod.sync_snapshot({'status': {'sync': {'revision': 'old', 'status': 'Synced'}}}, 'new')
        self.assertFalse(s['done'])
        s = mod.sync_snapshot({'status': {'sync': {'revision': 'new', 'status': 'OutOfSync'}}}, 'new')
        self.assertFalse(s['done'])
        s = mod.sync_snapshot({'status': {'sync': {'revision': 'new', 'status': 'Synced'}}}, 'new')
        self.assertTrue(s['done'])

    def test_sync_error_only_terminal_for_requested_revision(self):
        app = {'status': {'operationState': {'phase': 'Failed', 'message': 'sync failed',
                                            'syncResult': {'revision': 'old'}}}}
        self.assertFalse(mod.sync_snapshot(app, 'new')['terminal'])
        self.assertTrue(mod.sync_snapshot(app, 'old')['terminal'])

    def test_wait_ready_requires_all_eight_rollouts_and_gateway(self):
        items = []
        for name in mod.EXPECTED_ROLLOUTS:
            items.append({'metadata': {'name': name, 'generation': 1},
                          'spec': {'replicas': 1},
                          'status': {'phase': 'Healthy', 'availableReplicas': 1,
                                     'observedGeneration': 1}})
        gw = {'metadata': {'generation': 1},
              'status': {'observedGeneration': 1, 'availableReplicas': 1}}
        self.assertEqual(mod.readiness_report(items, gw), ([], []))
        incomplete = [x for x in items if x['metadata']['name'] != 'mariadb-a']
        self.assertTrue(any('mariadb-a: absent' in p for p in mod.readiness_report(incomplete, gw)[0]))

    def test_preflight_missing_controller_fails_before_gitops_apply(self):
        def fake_get(*args, **kwargs):
            if args[:2] == ('get', 'namespace'):
                return {'metadata': {'labels': {'istio-discovery': 'enabled',
                                                'istio-injection': 'enabled'}}}
            if 'secret' in args:
                return {'data': {'password': 'placeholder', 'root-password': 'placeholder'}}
            if 'rolloutmanager' in args:
                return {'items': []}
            raise AssertionError(args)
        with patch.object(mod, 'oc_json', side_effect=fake_get):
            with self.assertRaisesRegex(mod.BootstrapError, 'No Available cluster-scoped RolloutManager'):
                mod.preflight('full-multi', 'openshift-gitops')

    def test_wait_ready_rejects_active_admission_failure(self):
        rs = {'metadata': {'name': 'ingress-new'}, 'spec': {'replicas': 1},
              'status': {'conditions': [{'status': 'True', 'reason': 'FailedCreate',
                                        'message': 'port 15090 reserved'}]}}
        def fake_get(*args, **kwargs):
            if 'rollouts.argoproj.io' in args:
                return {'items': []}
            if 'deployment' in args:
                return {'metadata': {'generation': 1}, 'status': {'availableReplicas': 0}}
            if 'replicasets' in args:
                return {'items': [rs]}
            raise AssertionError(args)
        with patch.object(mod, 'oc_json', side_effect=fake_get):
            with self.assertRaisesRegex(mod.BootstrapError, 'port 15090 reserved'):
                mod.wait_ready('full-multi', 5, 1)

    def test_degraded_rollout_is_terminal(self):
        item = {'metadata': {'name': 'mariadb-a', 'generation': 1},
                'spec': {'replicas': 1},
                'status': {'phase': 'Degraded', 'availableReplicas': 0, 'observedGeneration': 1}}
        issues, degraded = mod.readiness_report([item],
            {'metadata': {'generation': 1}, 'status': {'availableReplicas': 1, 'observedGeneration': 1}})
        self.assertTrue(issues)
        self.assertIn('mariadb-a: phase Degraded', degraded)


if __name__ == '__main__':
    unittest.main()
