import importlib.util
from pathlib import Path
import unittest

import yaml

ROOT = Path(__file__).resolve().parent.parent
D = ROOT / 'full-multi'
spec = importlib.util.spec_from_file_location('ambient_bootstrap', ROOT / 'scripts/full-multi-bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


def load(name):
    return yaml.safe_load((D / name).read_text())


def route_backend(route, headers):
    """Gateway API's header-count precedence, then rule order for equal matches."""
    matches = []
    for i, rule in enumerate(route['spec']['rules']):
        for match in rule.get('matches', [{}]):
            expected = match.get('headers', [])
            if all(headers.get(h['name']) == h['value'] for h in expected):
                matches.append((-len(expected), i, rule['backendRefs']))
    return min(matches)[2]


class AmbientContractTests(unittest.TestCase):
    def test_resources_and_application_are_isolated_in_ambient_namespace(self):
        app = yaml.safe_load((ROOT / 'argocd/application-full-multi.yaml').read_text())
        self.assertEqual(app['metadata']['name'], 'multi-ambient')
        self.assertEqual(app['spec']['destination']['namespace'], 'multi-ambient')
        self.assertEqual(app['spec']['source']['targetRevision'], 'multi-ambient')
        kustomization = load('kustomization.yaml')
        for name in kustomization['resources']:
            for obj in yaml.safe_load_all((D / name).read_text()):
                if obj['kind'] == 'Namespace':
                    self.assertEqual(obj['metadata']['name'], 'multi-ambient')
                else:
                    self.assertEqual(obj['metadata']['namespace'], 'multi-ambient', name)
                self.assertNotIn('.full-multi.svc.cluster.local', yaml.safe_dump(obj), name)

    def test_openshift_route_uses_ambient_name_and_gateway(self):
        route = load('route.yaml')
        self.assertEqual(route['kind'], 'Route')
        self.assertEqual(route['metadata']['name'], 'multi-ambient')
        self.assertEqual(route['metadata']['namespace'], 'multi-ambient')
        self.assertEqual(route['spec']['to']['name'], 'full-multi-gateway-istio')

    def test_ingress_header_precedence_and_weighted_defaults(self):
        route = load('front-httproute.yaml')
        for target in ('a', 'b', 'unknown', None):
            for track in ('stable', 'canary', None):
                headers = {}
                if target is not None:
                    headers['x-app-target'] = target
                if track is not None:
                    headers['x-front-track'] = track
                app = 'b' if target == 'b' else 'a'
                refs = route_backend(route, headers)
                expected = ([f'front-{app}-{track}'] if track else
                            [f'front-{app}-stable', f'front-{app}-canary'])
                self.assertEqual([r['name'] for r in refs], expected, (target, track))
                if track is None:
                    self.assertEqual([r['weight'] for r in refs], [50, 50])

    def test_waypoint_routes_preserve_backend_and_db_selection(self):
        for app in 'ab':
            backend = load(f'backend-{app}-httproute.yaml')
            self.assertEqual(backend['spec']['parentRefs'][0]['name'], f'backend-{app}-stable')
            for track in ('stable', 'canary'):
                self.assertEqual(route_backend(backend, {'x-backend-track': track})[0]['name'],
                                 f'backend-{app}-{track}')
            db = load(f'crud-{app}-httproute.yaml')
            for value, expected in [('blue', 'blue'), ('stable', 'blue'),
                                    ('green', 'green'), ('canary', 'green'),
                                    ('other', 'active')]:
                refs = route_backend(db, {'x-db-track': value})
                self.assertEqual(refs[0]['name'], f'crud-{app}-{expected}')

    def test_injection_removed_and_ambient_labeled(self):
        labels = load('namespace.yaml')['metadata']['labels']
        self.assertEqual(labels['istio.io/dataplane-mode'], 'ambient')
        self.assertNotIn('istio-injection', labels)
        for path in list(D.glob('rollout-*.yaml')) + list(D.glob('statefulset-mariadb-*.yaml')):
            self.assertNotIn('sidecar.istio.io/inject', path.read_text(), path.name)

    def test_gateway_and_route_status_must_match_generation(self):
        condition = lambda kind: {'type': kind, 'status': 'True', 'observedGeneration': 2}
        gateway = {'metadata': {'generation': 2}, 'status': {'conditions': [condition('Programmed')]}}
        route = {'metadata': {'generation': 2}, 'status': {'parents': [{'conditions': [condition('Accepted'), condition('ResolvedRefs')]}]}}
        gateways = {name: gateway for name in ('full-multi-gateway', 'full-multi-waypoint')}
        names = ['full-multi-front-router'] + [f'{tier}-{app}-router' for tier in ('backend', 'crud') for app in 'ab']
        routes = {name: route for name in names}
        self.assertEqual(bootstrap.gateway_api_readiness(gateways, routes), [])
        routes = dict(routes)
        routes['crud-a-router'] = {'metadata': {'generation': 3}, 'status': route['status']}
        self.assertTrue(bootstrap.gateway_api_readiness(gateways, routes))

    def test_paused_rollout_blocks_cutover(self):
        items = [{'metadata': {'name': name, 'generation': 1},
                  'spec': {}, 'status': {'phase': 'Healthy', 'observedGeneration': 1}}
                 for name in bootstrap.EXPECTED_ROLLOUTS]
        bootstrap.check_cutover_rollouts(items)
        items[0]['status']['phase'] = 'Paused'
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.check_cutover_rollouts(items)
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.check_cutover_rollouts(items[:-1])
        with self.assertRaises(bootstrap.BootstrapError):
            bootstrap.check_cutover_rollouts([])
        bootstrap.check_cutover_rollouts([], allow_empty=True)


if __name__ == '__main__':
    unittest.main()
