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

    def test_existing_rbac_conflict_is_not_overwritten(self):
        with patch.object(mod, 'get', return_value={'rules': [{'verbs': ['*']}]}), patch.object(mod, 'create') as create:
            with self.assertRaises(mod.Failure):
                mod.ensure_rbac('openshift-gitops', 'argo-rollouts')
            create.assert_not_called()


if __name__ == '__main__':
    unittest.main()
