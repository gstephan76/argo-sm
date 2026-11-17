import importlib.util
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location('ambient_network', ROOT / 'scripts/full-multi-ambient-network.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class AmbientNetworkTests(unittest.TestCase):
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
