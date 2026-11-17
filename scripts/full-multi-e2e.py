#!/usr/bin/env python3
"""Read-only ambient end-to-end check for an existing Blue or paused demo."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, ROOT / 'scripts' / file)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


bootstrap = module('full_multi_bootstrap_e2e', 'full-multi-bootstrap.py')
workflow = module('full_multi_workflow_e2e', 'full-multi-global.py')


def check_weights(route, stable, canary, expected):
    # A weighted rule contains both services; forced-track rules contain one.
    pairs = []
    for rule in route.get('spec', {}).get('rules', []):
        refs = rule.get('backendRefs') or []
        if {ref.get('name') for ref in refs} == {stable, canary} and len(refs) == 2:
            pairs.append({ref['name']: ref.get('weight', 1) for ref in refs})
    if len(pairs) != 1 or pairs[0] != {stable: 100 - expected, canary: expected}:
        raise workflow.Failure(f'{stable}/{canary}: expected one weighted rule at {100-expected}/{expected}; got {pairs}')


def check_routes(state):
    ns = 'multi-ambient'
    bootstrap.preflight(ns, 'openshift-gitops')
    bootstrap.preflight_ambient_platform(ns)
    gateways = {x['metadata']['name']: x for x in
                bootstrap.oc_json('-n', ns, 'get', 'gateways.gateway.networking.k8s.io').get('items', [])}
    routes = {x['metadata']['name']: x for x in
              bootstrap.oc_json('-n', ns, 'get', 'httproutes.gateway.networking.k8s.io').get('items', [])}
    issues = bootstrap.gateway_api_readiness(gateways, routes)
    if issues:
        raise workflow.Failure('; '.join(issues))
    expected = 50 if state == 'paused' else 0
    for app in 'ab':
        check_weights(routes['full-multi-front-router'], f'front-{app}-stable', f'front-{app}-canary', expected)
        check_weights(routes[f'backend-{app}-router'], f'backend-{app}-stable', f'backend-{app}-canary', expected)
    app = bootstrap.oc_json('-n', 'openshift-gitops', 'get', 'application', 'multi-ambient')
    spec = app.get('spec') or {}
    source = spec.get('source') or {}
    destination = spec.get('destination') or {}
    sync = (app.get('status') or {}).get('sync') or {}
    if (source.get('targetRevision') != 'multi-ambient' or source.get('path') != 'full-multi' or
            source.get('repoURL') != 'https://github.com/gstephan76/argo-sm.git' or
            destination.get('namespace') != ns or destination.get('server') != 'https://kubernetes.default.svc' or
            sync.get('status') != 'Synced' or not sync.get('revision')):
        raise workflow.Failure('Argo CD Application has the wrong source/destination or is not Synced')
    print('PASS ambient Gateway/HTTPRoute readiness, configured traffic weights and GitOps sync', flush=True)


def check_forced_tracks():
    route = workflow.ocjson('-n', 'multi-ambient', 'get', 'route', 'full-multi')
    host = route.get('spec', {}).get('host')
    if not host:
        raise workflow.Failure('Route/full-multi has no host')
    for app in 'ab':
        for front in ('stable', 'canary'):
            for backend in ('stable', 'canary'):
                for db in ('blue', 'green'):
                    raw = workflow.call(['curl', '-q', '-kfsS', '--connect-timeout', '5', '--max-time', '20',
                                         '-H', f'x-app-target: {app}', '-H', f'x-front-track: {front}',
                                         '-H', f'x-backend-track: {backend}', '-H', f'x-db-track: {db}',
                                         f'https://{host}/api/books'], capture=True, timeout=30)
                    try:
                        result = json.loads(raw)
                    except ValueError as exc:
                        raise workflow.Failure('Data path returned invalid JSON') from exc
                    expected = {'application': app.upper(), 'database_track': db,
                                'database_host': f'mariadb-{app}-{db}.multi-ambient.svc.cluster.local',
                                'frontend_release': 'BLUE' if front == 'stable' else 'YELLOW',
                                'backend_release': 'BLUE' if backend == 'stable' else 'YELLOW'}
                    for key, value in expected.items():
                        if result.get(key) != value:
                            raise workflow.Failure(f'{app}/{front}/{backend}/{db}: {key} expected {value!r}, got {result.get(key)!r}')
                    books = result.get('books') or []
                    title = 'Pride and Prejudice' if db == 'blue' else 'Memórias Póstumas de Brás Cubas'
                    if len(books) != 20 or books[0].get('title') != title:
                        raise workflow.Failure(f'{app}/{front}/{backend}/{db}: unexpected book dataset')
                    print(f'PASS forced path app={app} front={front} backend={backend} db={db}', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', required=True, choices=('blue', 'paused'))
    args = parser.parse_args()
    try:
        check_routes(args.state)
        if args.state == 'blue':
            workflow.baseline_e2e()
        else:
            workflow.paused_e2e()
            check_forced_tracks()
        print('PASS application E2E. Tempo trace retrieval and Kiali scrape targets require separate verification.')
        return 0
    except (bootstrap.BootstrapError, workflow.Failure, KeyError, ValueError) as exc:
        print('FAIL E2E:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
