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

    def test_protected_migration_accepts_only_legacy_resources_to_prune(self):
        legacy = {'group': 'argoproj.io', 'kind': 'Rollout', 'name': 'mariadb-a',
                  'namespace': 'multi-ambient', 'status': 'OutOfSync', 'requiresPruning': True}
        app = {'spec': {'syncPolicy': {'automated': {'prune': False}}},
               'status': {'sync': {'revision': 'new', 'status': 'OutOfSync'},
                          'resources': [legacy]}}
        self.assertFalse(mod.sync_snapshot(app, 'new')['done'])
        self.assertTrue(mod.sync_snapshot(app, 'new', True)['done'])
        app['status']['resources'].append({'kind': 'Rollout', 'name': 'crud-a', 'status': 'OutOfSync'})
        self.assertFalse(mod.sync_snapshot(app, 'new', True)['done'])
        app['status']['resources'].pop()
        app['status']['operationState'] = {'phase': 'Failed', 'syncResult': {'revision': 'new'}}
        self.assertFalse(mod.sync_snapshot(app, 'new', True)['done'])

    def test_deploy_refuses_to_reenable_disabled_pruning(self):
        app = {'spec': {'source': {'path': 'full-multi', 'targetRevision': 'multi-ambient',
                                  'repoURL': 'https://github.com/gstephan76/argo-sm.git'},
                        'destination': {'server': 'https://kubernetes.default.svc', 'namespace': 'multi-ambient'},
                        'syncPolicy': {'automated': {'prune': False}}}}
        with patch.object(mod, 'oc_json', return_value=app):
            with self.assertRaisesRegex(mod.BootstrapError, 'pruning disabled'):
                mod.preflight_deploy('multi-ambient', 'openshift-gitops', 'multi-ambient')

    def test_deploy_refuses_existing_application_with_other_destination(self):
        app = {'spec': {'source': {'path': 'full-multi', 'targetRevision': 'multi-ambient',
                                  'repoURL': 'https://github.com/gstephan76/argo-sm.git'},
                        'destination': {'server': 'https://kubernetes.default.svc', 'namespace': 'full-multi'}}}
        with patch.object(mod, 'oc_json', return_value=app):
            with self.assertRaisesRegex(mod.BootstrapError, 'refusing takeover'):
                mod.preflight_deploy('multi-ambient', 'openshift-gitops', 'multi-ambient')

    def test_deploy_refuses_legacy_database_rollout(self):
        def fake_get(*args, **kwargs):
            if 'application' in args:
                return None
            if 'namespace' in args:
                return {'metadata': {'name': 'full-multi'}}
            return {'items': [{'metadata': {'name': 'mariadb-a'}}]}
        with patch.object(mod, 'oc_json', side_effect=fake_get):
            with self.assertRaisesRegex(mod.BootstrapError, 'Legacy MariaDB'):
                mod.preflight_deploy('multi-ambient', 'openshift-gitops', 'multi-ambient')

    def test_readiness_requires_all_six_rollouts_and_gateway(self):
        items = []
        for name in mod.EXPECTED_ROLLOUTS:
            items.append({'metadata': {'name': name, 'generation': 1},
                          'spec': {'replicas': 1},
                          'status': {'phase': 'Healthy', 'availableReplicas': 1,
                                     'observedGeneration': 1}})
        gw = {'metadata': {'generation': 1},
              'status': {'observedGeneration': 1, 'availableReplicas': 1}}
        self.assertEqual(mod.readiness_report(items, gw), ([], []))
        incomplete = [x for x in items if x['metadata']['name'] != 'crud-a']
        self.assertTrue(any('crud-a: absent' in p for p in mod.readiness_report(incomplete, gw)[0]))

    def test_preflight_missing_controller_fails_before_gitops_apply(self):
        def fake_get(*args, **kwargs):
            if args[:2] == ('get', 'namespace'):
                return {'metadata': {'labels': {'istio-discovery': 'enabled',
                                                'istio.io/dataplane-mode': 'ambient',
                                                'istio.io/use-waypoint': 'full-multi-waypoint'}}}
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
            if 'statefulsets.apps' in args or 'persistentvolumeclaims' in args:
                return {'items': []}
            raise AssertionError(args)
        with patch.object(mod, 'oc_json', side_effect=fake_get):
            with self.assertRaisesRegex(mod.BootstrapError, 'port 15090 reserved'):
                mod.wait_ready('full-multi', 5, 1)

    def test_degraded_rollout_is_terminal(self):
        item = {'metadata': {'name': 'crud-a', 'generation': 1},
                'spec': {'replicas': 1},
                'status': {'phase': 'Degraded', 'availableReplicas': 0, 'observedGeneration': 1}}
        issues, degraded = mod.readiness_report([item],
            {'metadata': {'generation': 1}, 'status': {'availableReplicas': 1, 'observedGeneration': 1}})
        self.assertTrue(issues)
        self.assertIn('crud-a: phase Degraded', degraded)


if __name__ == '__main__':
    unittest.main()
