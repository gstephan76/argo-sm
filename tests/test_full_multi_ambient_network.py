import importlib.util
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('ambient_network', ROOT / 'scripts/full-multi-ambient-network.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class AmbientNetworkTests(unittest.TestCase):
    def network(self, enabled=False):
        return {'metadata': {'uid': 'network-uid', 'resourceVersion': '8'},
                'spec': {'defaultNetwork': {'type': 'OVNKubernetes',
                         'ovnKubernetesConfig': {'gatewayConfig': {'routingViaHost': enabled}}}}}

    def test_existing_gateway_is_verified_without_another_patch(self):
        with patch.object(mod, 'oc') as command:
            mod.ensure_gateway(self.network(True))
        command.assert_not_called()

    def test_gateway_transaction_ignores_status_versions_and_guards_spec(self):
        network = self.network()
        with patch.object(mod, 'oc') as command:
            mod.ensure_gateway(network)
        operations = json.loads(command.call_args.args[-1])
        self.assertEqual(operations[:2], [
            {'op': 'test', 'path': '/metadata/uid', 'value': 'network-uid'},
            {'op': 'test', 'path': '/spec', 'value': network['spec']}])

    def test_lost_patch_response_checks_effect_without_replaying_write(self):
        with patch.object(mod, 'oc', side_effect=mod.Failure('connection reset')) as command, \
             patch.object(mod, 'read', return_value=self.network(True)):
            mod.ensure_gateway(self.network())
        command.assert_called_once()

    def test_unapplied_or_replaced_gateway_stops(self):
        replaced = copy.deepcopy(self.network(True))
        replaced['metadata']['uid'] = 'other'
        for latest in [self.network(), replaced]:
            with self.subTest(latest=latest), patch.object(mod, 'oc', side_effect=mod.Failure('patch rejected')) as command, \
                 patch.object(mod, 'read', return_value=latest):
                with self.assertRaises(mod.Failure):
                    mod.ensure_gateway(self.network())
                command.assert_called_once()

    def test_gateway_requires_explicit_boolean_and_ovn(self):
        net = {'spec': {'defaultNetwork': {'type': 'OVNKubernetes', 'ovnKubernetesConfig': {'gatewayConfig': {'routingViaHost': False}}}}}
        self.assertFalse(mod.gateway_setting(net))
        net['spec']['defaultNetwork']['ovnKubernetesConfig']['gatewayConfig']['routingViaHost'] = True
        self.assertTrue(mod.gateway_setting(net))
        net['spec']['defaultNetwork']['type'] = 'other'
        with self.assertRaises(mod.Failure):
            mod.gateway_setting(net)

    def test_ready_requires_effective_local_mode_and_complete_daemonset(self):
        net = {'spec': {'defaultNetwork': {'type': 'OVNKubernetes', 'ovnKubernetesConfig': {'gatewayConfig': {'routingViaHost': True}}}}}
        operator = {'status': {'conditions': [{'type': k, 'status': v} for k, v in [('Available', 'True'), ('Progressing', 'False'), ('Degraded', 'False')]]}}
        ds = {'metadata': {'generation': 3}, 'status': {'desiredNumberScheduled': 5, 'updatedNumberScheduled': 5, 'numberReady': 5, 'numberAvailable': 5, 'numberMisscheduled': 0, 'observedGeneration': 3}}
        cm = {'data': {'ovnkube.conf': '[gateway]\nmode=local\n'}}
        self.assertTrue(mod.ready(net, operator, ds, cm))
        cm['data']['ovnkube.conf'] = '[gateway]\nmode=shared\n'
        self.assertFalse(mod.ready(net, operator, ds, cm))
        cm['data']['ovnkube.conf'] = '[gateway]\nmode=local\n'
        ds['status']['updatedNumberScheduled'] = 4
        self.assertFalse(mod.ready(net, operator, ds, cm))


if __name__ == '__main__':
    unittest.main()
