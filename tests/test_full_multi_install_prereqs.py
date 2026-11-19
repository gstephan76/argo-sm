"""Offline checks for idempotent shared prerequisite installation."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('multi_install', ROOT / 'scripts/full-multi-install-prereqs.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class PrerequisiteTests(unittest.TestCase):
    def sso_binding(self):
        return {'roleRef': {'kind': 'ClusterRole', 'name': 'cluster-admin'},
                'subjects': [{'kind': 'User', 'name': 'admin'}]}

    def test_sso_admin_mapping_preserves_policy_and_default(self):
        rbac = {'policy': 'g, existing-group, role:readonly\n', 'defaultPolicy': '', 'scopes': '[groups, email]'}
        instance = {'metadata': {'resourceVersion': '8'}, 'spec': {'rbac': rbac}}
        with patch.object(mod, 'get', return_value={'metadata': {'name': 'admin'}}), \
             patch.object(mod, 'oc', return_value=json.dumps({'items': [self.sso_binding()]})), \
             patch.object(mod, 'wait_for', return_value=instance), patch.object(mod, 'patch') as update:
            mod.ensure_sso_admin()
        updated = update.call_args.args[-1][0]['value']
        self.assertEqual(updated['policy'], rbac['policy'] + 'g, admin, role:admin\n')
        self.assertEqual(updated['defaultPolicy'], '')
        self.assertEqual(json.loads(updated['scopes']), ['groups', 'email', 'preferred_username'])

    def test_sso_admin_mapping_rerun_does_not_patch(self):
        instance = {'spec': {'rbac': {'policy': 'g, admin, role:admin\n',
                                     'scopes': '[groups, preferred_username]'}}}
        with patch.object(mod, 'get', return_value={}), \
             patch.object(mod, 'oc', return_value=json.dumps({'items': [self.sso_binding()]})), \
             patch.object(mod, 'wait_for', return_value=instance), patch.object(mod, 'patch') as update:
            mod.ensure_sso_admin()
        update.assert_not_called()

    def test_sso_admin_grant_requires_existing_cluster_admin_identity(self):
        for user, bindings in [(None, [self.sso_binding()]), ({}, [])]:
            with self.subTest(user=user, bindings=bindings), patch.object(mod, 'get', return_value=user), \
                 patch.object(mod, 'oc', return_value=json.dumps({'items': bindings})), patch.object(mod, 'patch') as update:
                with self.assertRaisesRegex(mod.Failure, 'unverified'):
                    mod.ensure_sso_admin()
                update.assert_not_called()

    def test_sso_admin_refuses_conflicting_policy_or_malformed_scopes(self):
        for rbac in [{'policy': 'g, admin, role:readonly', 'scopes': '[groups]'},
                     {'scopes': 'unexpected'}]:
            with self.subTest(rbac=rbac), patch.object(mod, 'get', return_value={}), \
                 patch.object(mod, 'oc', return_value=json.dumps({'items': [self.sso_binding()]})), \
                 patch.object(mod, 'wait_for', return_value={'spec': {'rbac': rbac}}), patch.object(mod, 'patch') as update:
                with self.assertRaises(mod.Failure):
                    mod.ensure_sso_admin()
                update.assert_not_called()

    def test_manager_recovers_status_race_then_rerun_does_not_write(self):
        manager = self.manager_snapshot()
        manager['status'] = {'phase': 'Available', 'rolloutController': 'Available'}
        deployment = {'spec': {'template': {'spec': {'serviceAccountName': 'controller'}}},
                      'status': {'availableReplicas': 1}}
        writes = []

        def command(*args, data=None):
            if args[:2] == ('get', 'nodes'):
                return json.dumps({'items': [{'status': {'nodeInfo': {'architecture': 'amd64', 'operatingSystem': 'linux'}}}]})
            if args[:2] == ('get', 'rolloutmanagers.argoproj.io'):
                return json.dumps({'items': [manager]})
            if args[:2] == ('get', 'rolloutmanager'):
                return json.dumps(manager)
            if args[:2] == ('get', 'deployment'):
                return json.dumps(deployment)
            if args[0] == 'patch':
                operations = json.loads(args[-1])
                writes.append(operations)
                if len(writes) == 1:
                    manager['metadata']['resourceVersion'] = '9'
                    raise mod.Failure('The request is invalid')
                self.assertEqual(operations[0]['value'], manager['metadata']['resourceVersion'])
                manager['spec']['plugins'] = operations[1]['value']
                manager['metadata']['resourceVersion'] = '10'
                return 'patched'
            self.fail(f'Unexpected command: {args}')

        with patch.object(mod, 'oc', side_effect=command):
            self.assertEqual(mod.ensure_manager(), (mod.ARGO_NS, 'controller'))
            self.assertEqual(len(writes), 2)
            self.assertEqual(mod.ensure_manager(), (mod.ARGO_NS, 'controller'))
            self.assertEqual(len(writes), 2)

    def test_create_recovers_only_matching_object_after_uncertain_response(self):
        obj = {'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': 'demo', 'labels': {'owner': 'gitops'}}}
        for label, succeeds in [('gitops', True), ('other', False)]:
            live = {'apiVersion': 'v1', 'kind': 'Namespace',
                    'metadata': {'name': 'demo', 'uid': 'existing', 'labels': {'owner': label, 'default': 'value'}}}
            with self.subTest(label=label), patch.object(mod, 'oc', side_effect=mod.Failure('connection reset')) as command, \
                 patch.object(mod, 'get', return_value=live):
                if succeeds:
                    mod.create(obj)
                else:
                    with self.assertRaises(mod.Failure):
                        mod.create(obj)
                command.assert_called_once()

    def manager_snapshot(self, version='8'):
        return {'apiVersion': 'argoproj.io/v1alpha1', 'kind': 'RolloutManager',
                'metadata': {'name': 'rollout-manager', 'namespace': 'openshift-gitops',
                             'uid': 'manager-uid', 'resourceVersion': version, 'generation': 1},
                'spec': {'namespaceScoped': False}}

    def test_patch_retries_status_race_with_fresh_version(self):
        manager = self.manager_snapshot()
        latest = self.manager_snapshot('9')
        latest['status'] = {'phase': 'Available', 'rolloutController': 'Available'}
        latest['metadata']['managedFields'] = [{'manager': 'operator', 'subresource': 'status'}]
        operations = [{'op': 'add', 'path': '/spec/plugins',
                       'value': {'trafficManagement': [{'name': mod.PLUGIN, 'location': mod.PLUGIN_URL}]}}]
        invalid = mod.Failure('The request is invalid: the server rejected our request due to an error in our request')
        with patch.object(mod, 'oc', side_effect=[invalid, 'patched']) as command, \
             patch.object(mod, 'get', return_value=latest) as read:
            mod.patch('rolloutmanager', 'rollout-manager', mod.ARGO_NS, manager, operations)
        read.assert_called_once_with('rolloutmanager', 'rollout-manager', mod.ARGO_NS)
        patches = [json.loads(call.args[-1]) for call in command.call_args_list]
        self.assertEqual([p[0] for p in patches], [
            {'op': 'test', 'path': '/metadata/resourceVersion', 'value': version}
            for version in ('8', '9')])
        self.assertEqual([p[1:] for p in patches], [operations, operations])

    def test_patch_refuses_concurrent_configuration_or_identity_changes(self):
        manager = self.manager_snapshot()
        cases = [None]
        for key, value in [('uid', 'replacement-uid'), ('labels', {'other': 'owner'}),
                           ('annotations', {'other': 'edit'}), ('generation', 2),
                           ('deletionTimestamp', '2026-11-19T15:20:00Z')]:
            latest = self.manager_snapshot('9')
            latest['metadata'][key] = value
            cases.append(latest)
        latest = self.manager_snapshot('9')
        latest['spec']['plugins'] = {'metric': [{'name': 'other', 'location': 'file:///other'}]}
        cases.append(latest)
        for latest in cases:
            with self.subTest(latest=latest), \
                 patch.object(mod, 'oc', side_effect=mod.Failure('patch rejected')) as command, \
                 patch.object(mod, 'get', return_value=latest):
                with self.assertRaisesRegex(mod.Failure, 'patch rejected'):
                    mod.patch('rolloutmanager', 'rollout-manager', mod.ARGO_NS, manager, [])
                command.assert_called_once()

    def test_patch_does_not_retry_without_resource_version_change(self):
        manager = self.manager_snapshot()
        with patch.object(mod, 'oc', side_effect=mod.Failure('admission denied')) as command, \
             patch.object(mod, 'get', return_value=self.manager_snapshot()):
            with self.assertRaisesRegex(mod.Failure, 'admission denied'):
                mod.patch('rolloutmanager', 'rollout-manager', mod.ARGO_NS, manager, [])
        command.assert_called_once()

    def test_patch_status_retries_are_bounded(self):
        manager = self.manager_snapshot()
        updates = [self.manager_snapshot(v) for v in ('9', '10')]
        with patch.object(mod, 'oc', side_effect=mod.Failure('patch rejected')) as command, \
             patch.object(mod, 'get', side_effect=updates) as read:
            with self.assertRaisesRegex(mod.Failure, 'patch rejected'):
                mod.patch('rolloutmanager', 'rollout-manager', mod.ARGO_NS, manager, [])
        self.assertEqual(command.call_count, 3)
        self.assertEqual(read.call_count, 2)

    def test_console_add_preserves_other_plugins(self):
        console = {'metadata': {'resourceVersion': '42'}, 'spec': {'plugins': ['kiali', 'tempo']}}
        with patch.object(mod, 'wait_for'), patch.object(mod, 'get', return_value=console), patch.object(mod, 'patch') as update:
            mod.ensure_console()
        self.assertEqual(update.call_args.args[-1][0]['value'], ['kiali', 'tempo', 'gitops-plugin'])

    def test_console_rerun_does_not_patch(self):
        console = {'metadata': {'resourceVersion': '42'}, 'spec': {'plugins': ['kiali', 'gitops-plugin']}}
        with patch.object(mod, 'wait_for'), patch.object(mod, 'get', return_value=console), patch.object(mod, 'patch') as update:
            mod.ensure_console()
        update.assert_not_called()

    def test_refuses_multiple_or_namespaced_managers(self):
        cluster = {'spec': {'namespaceScoped': False}}
        self.assertIs(mod.select_manager([cluster]), cluster)
        with self.assertRaises(mod.Failure):
            mod.select_manager([cluster, cluster])
        with self.assertRaises(mod.Failure):
            mod.select_manager([{'spec': {'namespaceScoped': True}}])

    def test_gateway_plugin_preserves_existing_traffic_and_metric_plugins(self):
        manager = {'metadata': {'name': 'rollout-manager', 'namespace': 'openshift-gitops', 'resourceVersion': '8'},
                   'spec': {'namespaceScoped': False, 'plugins': {'trafficManagement': [{'name': 'existing', 'location': 'file:///existing'}],
                                                                  'metric': [{'name': 'metrics', 'location': 'file:///metrics'}]}}}
        nodes = {'items': [{'status': {'nodeInfo': {'architecture': 'amd64', 'operatingSystem': 'linux'}}}]}
        deployment = {'spec': {'template': {'spec': {'serviceAccountName': 'controller'}}}}
        with patch.object(mod, 'oc', side_effect=[json.dumps(nodes), json.dumps({'items': [manager]})]), \
             patch.object(mod, 'wait_for', side_effect=[manager, deployment]), patch.object(mod, 'patch') as update:
            self.assertEqual(mod.ensure_manager(), ('openshift-gitops', 'controller'))
        plugins = update.call_args.args[-1][0]['value']
        self.assertEqual(plugins['metric'], manager['spec']['plugins']['metric'])
        self.assertEqual(plugins['trafficManagement'][0]['name'], 'existing')
        self.assertEqual(plugins['trafficManagement'][1]['location'], mod.PLUGIN_URL)

    def test_namespace_preserves_labels_and_refuses_different_owner(self):
        ns = {'metadata': {'resourceVersion': '9', 'labels': {'existing': 'yes'}}}
        with patch.object(mod, 'get', return_value=ns), patch.object(mod, 'patch') as update:
            mod.ensure_namespace()
        labels = update.call_args.args[-1][0]['value']
        self.assertEqual(labels['existing'], 'yes')
        self.assertEqual(labels['argocd.argoproj.io/managed-by'], 'openshift-gitops')
        ns['metadata']['labels']['argocd.argoproj.io/managed-by'] = 'another-argocd'
        with patch.object(mod, 'get', return_value=ns), patch.object(mod, 'patch') as update:
            with self.assertRaises(mod.Failure):
                mod.ensure_namespace()
            update.assert_not_called()

    def test_gitops_gateway_role_is_namespace_scoped_and_idempotent(self):
        controller = {'spec': {'template': {'spec': {'serviceAccountName': 'gitops-controller'}}}}
        with patch.object(mod, 'wait_for', return_value=controller), \
             patch.object(mod, 'get', side_effect=[None, None]), patch.object(mod, 'create') as create:
            mod.ensure_gitops_gateway_rbac()
        role, binding = (call.args[0] for call in create.call_args_list)
        self.assertEqual(role['metadata']['namespace'], 'multi-ambient')
        self.assertEqual(role['rules'][0]['resources'], ['gateways'])
        self.assertEqual(binding['subjects'][0]['name'], 'gitops-controller')
        with patch.object(mod, 'wait_for', return_value=controller), \
             patch.object(mod, 'get', side_effect=[role, binding]), patch.object(mod, 'create') as create:
            mod.ensure_gitops_gateway_rbac()
        create.assert_not_called()

    def test_waits_for_operator_owned_resource_before_reading_account(self):
        controller = {'spec': {'template': {'spec': {'serviceAccountName': 'gitops-controller'}}}}
        with patch.object(mod, 'get', side_effect=[None, {}, controller]), patch.object(mod.time, 'sleep') as sleep:
            result = mod.wait_for('service account', lambda: mod.get('statefulset', 'controller'),
                                  lambda x: bool(x.get('spec', {}).get('template', {}).get('spec', {}).get('serviceAccountName')))
        self.assertIs(result, controller)
        self.assertEqual(sleep.call_count, 2)

    def test_existing_rbac_conflict_is_not_overwritten(self):
        with patch.object(mod, 'get', return_value={'rules': [{'verbs': ['*']}]}), patch.object(mod, 'create') as create:
            with self.assertRaises(mod.Failure):
                mod.ensure_rbac('openshift-gitops', 'argo-rollouts')
            create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
