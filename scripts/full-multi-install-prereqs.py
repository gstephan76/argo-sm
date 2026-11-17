#!/usr/bin/env python3
"""Install or inspect the shared OpenShift GitOps prerequisites for multi-ambient.

The default is read-only. --apply creates the operator and one cluster-scoped
RolloutManager if absent, enables the supported GitOps console plugin, and
configures the Gateway API traffic plugin and namespace-scoped RBAC. Existing
conflicts are rejected; existing unrelated plugins and console entries survive.
No application, database, Secret, or Service Mesh installation is performed.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OP_NS = 'openshift-gitops-operator'
ARGO_NS = 'openshift-gitops'
APP_NS = 'multi-ambient'
PLUGIN = 'argoproj-labs/gatewayAPI'
PLUGIN_URL = ('https://github.com/argoproj-labs/rollouts-plugin-trafficrouter-gatewayapi/'
              'releases/download/v0.17.0/gatewayapi-plugin-linux-amd64')
RBAC_NAME = 'multi-ambient-gatewayapi-plugin'


class Failure(RuntimeError):
    pass


def oc(*args, data=None):
    try:
        p = subprocess.run(['oc', *args], input=data, text=True, capture_output=True, timeout=45)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f'oc failed: {exc}') from exc
    if p.returncode:
        raise Failure(f'oc {" ".join(args[:8])} failed: {p.stderr.strip()}')
    return p.stdout.strip()


def get(kind, name=None, namespace=None):
    args = ['get', kind]
    if name:
        args.append(name)
    if namespace:
        args += ['-n', namespace]
    args += ['--ignore-not-found', '-o', 'json']
    raw = oc(*args)
    return json.loads(raw) if raw else None


def create(obj):
    oc('create', '-f', '-', data=json.dumps(obj))
    print(f"CREATED {obj['kind']}/{obj['metadata']['name']}", flush=True)


def patch(kind, name, namespace, obj, operations):
    rv = obj['metadata']['resourceVersion']
    args = ['patch', kind, name]
    if namespace:
        args += ['-n', namespace]
    args += ['--type=json', '-p', json.dumps([{'op': 'test', 'path': '/metadata/resourceVersion', 'value': rv}] + operations)]
    oc(*args)
    print(f'UPDATED {kind}/{name}', flush=True)


def wait_for(description, read, predicate, seconds=600):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        value = read()
        if value is not None and predicate(value):
            print('READY ' + description, flush=True)
            return value
        time.sleep(5)
    raise Failure(f'Timed out waiting for {description}; inspect the cluster before retrying')


def ensure_namespace():
    ns = get('namespace', APP_NS)
    required = {'istio-discovery': 'enabled', 'istio.io/dataplane-mode': 'ambient',
                'istio.io/use-waypoint': 'full-multi-waypoint',
                'argocd.argoproj.io/managed-by': ARGO_NS}
    if ns is None:
        oc('create', '-f', str(ROOT / 'full-multi/namespace.yaml'))
        print(f'CREATED Namespace/{APP_NS}', flush=True)
        return
    labels = ns.get('metadata', {}).get('labels') or {}
    if labels.get('istio-injection') == 'enabled' or labels.get('istio.io/rev'):
        raise Failure(f'Namespace {APP_NS} enables sidecar injection; inspect it before proceeding')
    conflicts = [key for key, value in required.items() if key in labels and labels[key] != value]
    if conflicts:
        raise Failure('Existing namespace has conflicting labels: ' + ', '.join(conflicts))
    if any(labels.get(k) != v for k, v in required.items()):
        merged = dict(labels, **required)
        patch('namespace', APP_NS, None, ns,
              [{'op': 'add', 'path': '/metadata/labels', 'value': merged}])


def ensure_operator():
    # Query every namespace: avoid installing a second instance of the operator.
    all_subs = json.loads(oc('get', 'subscriptions.operators.coreos.com', '-A', '-o', 'json'))
    gitops = [s for s in all_subs.get('items', []) if s.get('spec', {}).get('name') == 'openshift-gitops-operator']
    if any(s['metadata']['namespace'] != OP_NS or s['metadata']['name'] != 'openshift-gitops-operator' for s in gitops):
        raise Failure('GitOps operator subscription already exists elsewhere; refuse a second installation')
    if not get('namespace', OP_NS):
        create({'apiVersion': 'v1', 'kind': 'Namespace', 'metadata': {'name': OP_NS}})
    groups = (get('operatorgroups.operators.coreos.com', namespace=OP_NS) or {}).get('items', [])
    if not groups:
        create({'apiVersion': 'operators.coreos.com/v1', 'kind': 'OperatorGroup',
                'metadata': {'name': OP_NS, 'namespace': OP_NS}, 'spec': {'upgradeStrategy': 'Default'}})
    elif len(groups) != 1 or groups[0].get('spec', {}).get('targetNamespaces'):
        raise Failure('Unexpected OperatorGroup in GitOps operator namespace; inspect before proceeding')
    sub = get('subscription.operators.coreos.com', 'openshift-gitops-operator', OP_NS)
    expected = {'channel': 'latest', 'installPlanApproval': 'Automatic', 'name': 'openshift-gitops-operator',
                'source': 'redhat-operators', 'sourceNamespace': 'openshift-marketplace'}
    if sub is None:
        create({'apiVersion': 'operators.coreos.com/v1alpha1', 'kind': 'Subscription',
                'metadata': {'name': 'openshift-gitops-operator', 'namespace': OP_NS}, 'spec': expected})
    elif any(sub.get('spec', {}).get(k) != v for k, v in expected.items()):
        raise Failure('Existing GitOps subscription differs from the approved latest-channel configuration')
    sub = wait_for('GitOps operator subscription',
                   lambda: get('subscription.operators.coreos.com', 'openshift-gitops-operator', OP_NS),
                   lambda x: bool(x.get('status', {}).get('installedCSV')))
    csv = sub['status']['installedCSV']
    wait_for('GitOps operator CSV', lambda: get('clusterserviceversion.operators.coreos.com', csv, OP_NS),
             lambda x: x.get('status', {}).get('phase') == 'Succeeded')
    for name in ('applications.argoproj.io', 'rollouts.argoproj.io', 'rolloutmanagers.argoproj.io'):
        wait_for('CRD ' + name, lambda n=name: get('crd', n), lambda x: any(
            c.get('type') == 'Established' and c.get('status') == 'True'
            for c in x.get('status', {}).get('conditions', [])))
    wait_for('GitOps namespace', lambda: get('namespace', ARGO_NS), lambda x: True)


def ensure_console():
    wait_for('GitOps console plugin', lambda: get('consoleplugin.console.openshift.io', 'gitops-plugin'), lambda x: True)
    console = get('console.operator.openshift.io', 'cluster')
    if not console:
        raise Failure('OpenShift console operator configuration is missing')
    current = (console.get('spec') or {}).get('plugins') or []
    if 'gitops-plugin' not in current:
        patch('console.operator.openshift.io', 'cluster', None, console,
              [{'op': 'add', 'path': '/spec/plugins', 'value': current + ['gitops-plugin']}])
    else:
        print('OK GitOps console plugin already enabled', flush=True)


def select_manager(items):
    if not items:
        return None
    if len(items) != 1 or (items[0].get('spec') or {}).get('namespaceScoped') is not False:
        raise Failure('Expected one cluster-scoped RolloutManager; inspect existing managers')
    return items[0]


def ensure_manager():
    nodes = json.loads(oc('get', 'nodes', '-o', 'json'))['items']
    if not nodes or any(n.get('status', {}).get('nodeInfo', {}).get('architecture') != 'amd64' or
                        n.get('status', {}).get('nodeInfo', {}).get('operatingSystem') != 'linux' for n in nodes):
        raise Failure('The selected Gateway API plugin binary requires Linux amd64 nodes')
    items = json.loads(oc('get', 'rolloutmanagers.argoproj.io', '-A', '-o', 'json')).get('items', [])
    manager = select_manager(items)
    if manager is None:
        oc('create', '-f', str(ROOT / 'platform-monitoring/rollout-manager-cluster.yaml'))
        print('CREATED cluster-scoped RolloutManager', flush=True)
        manager = get('rolloutmanager', 'rollout-manager', ARGO_NS)
    spec = manager.get('spec') or {}
    plugins = spec.get('plugins') or {}
    traffic = plugins.get('trafficManagement') or []
    matching = [p for p in traffic if p.get('name') == PLUGIN]
    if len(matching) > 1 or (matching and matching[0].get('location') != PLUGIN_URL):
        raise Failure('Existing Gateway API plugin configuration conflicts; inspect before proceeding')
    if not matching:
        updated = dict(plugins, trafficManagement=traffic + [{'name': PLUGIN, 'location': PLUGIN_URL}])
        patch('rolloutmanager', manager['metadata']['name'], manager['metadata']['namespace'], manager,
              [{'op': 'add', 'path': '/spec/plugins', 'value': updated}])
    name, ns = manager['metadata']['name'], manager['metadata']['namespace']
    wait_for('cluster-scoped RolloutManager', lambda: get('rolloutmanager', name, ns),
             lambda x: x.get('status', {}).get('phase') == 'Available' and
             x.get('status', {}).get('rolloutController') == 'Available')
    deployment = wait_for('Rollouts controller', lambda: get('deployment', 'argo-rollouts', ns),
                          lambda x: x.get('status', {}).get('availableReplicas', 0) >= 1)
    service_account = deployment.get('spec', {}).get('template', {}).get('spec', {}).get('serviceAccountName')
    if not service_account:
        raise Failure('Rollouts controller service account is missing')
    return ns, service_account


def ensure_rbac(owner_ns, account):
    role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
            'metadata': {'name': RBAC_NAME, 'namespace': APP_NS},
            'rules': [{'apiGroups': [''], 'resources': ['services'], 'verbs': ['get']},
                      {'apiGroups': ['gateway.networking.k8s.io'], 'resources': ['httproutes'],
                       'verbs': ['get', 'list', 'update', 'patch']}]}
    binding = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
               'metadata': {'name': RBAC_NAME, 'namespace': APP_NS},
               'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': RBAC_NAME},
               'subjects': [{'kind': 'ServiceAccount', 'name': account, 'namespace': owner_ns}]}
    for obj, fields in [(role, ('rules',)), (binding, ('roleRef', 'subjects'))]:
        live = get(obj['kind'].lower(), RBAC_NAME, APP_NS)
        if live is None:
            create(obj)
        elif any(live.get(k) != obj[k] for k in fields):
            raise Failure(f'Existing {obj["kind"]}/{RBAC_NAME} differs; inspect before proceeding')
        else:
            print(f'OK {obj["kind"]}/{RBAC_NAME} already configured', flush=True)


def ensure_gitops_gateway_rbac():
    controller = get('statefulset', 'openshift-gitops-application-controller', ARGO_NS)
    account = (((controller or {}).get('spec') or {}).get('template') or {}).get('spec', {}).get('serviceAccountName')
    if not account:
        raise Failure('Cannot identify the GitOps application controller service account')
    name = 'multi-ambient-gitops-gateway'
    role = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'Role',
            'metadata': {'name': name, 'namespace': APP_NS},
            'rules': [{'apiGroups': ['gateway.networking.k8s.io'], 'resources': ['gateways'],
                       'verbs': ['get', 'list', 'watch', 'create', 'update', 'patch', 'delete']}]}
    binding = {'apiVersion': 'rbac.authorization.k8s.io/v1', 'kind': 'RoleBinding',
               'metadata': {'name': name, 'namespace': APP_NS},
               'roleRef': {'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role', 'name': name},
               'subjects': [{'kind': 'ServiceAccount', 'name': account, 'namespace': ARGO_NS}]}
    for obj, fields in [(role, ('rules',)), (binding, ('roleRef', 'subjects'))]:
        live = get(obj['kind'].lower(), name, APP_NS)
        if live is None:
            create(obj)
        elif any(live.get(k) != obj[k] for k in fields):
            raise Failure(f'Existing {obj["kind"]}/{name} differs; inspect before proceeding')
        else:
            print(f'OK {obj["kind"]}/{name} already configured', flush=True)


def check():
    for kind, name, namespace in [('namespace', OP_NS, None), ('namespace', ARGO_NS, None),
                                  ('namespace', APP_NS, None), ('crd', 'applications.argoproj.io', None),
                                  ('crd', 'rollouts.argoproj.io', None), ('crd', 'rolloutmanagers.argoproj.io', None),
                                  ('crd', 'httproutes.gateway.networking.k8s.io', None)]:
        print(('PRESENT ' if get(kind, name, namespace) else 'MISSING ') + f'{kind}/{name}')
    console = get('console.operator.openshift.io', 'cluster')
    print('GitOps console plugin:', 'enabled' if 'gitops-plugin' in ((console or {}).get('spec', {}).get('plugins') or []) else 'not enabled')
    print('Read-only check: mesh, plugin process, controller permissions and workload traffic require separate verification.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read-only inspection (default)')
    mode.add_argument('--apply', action='store_true', help='Install/configure prerequisites')
    args = parser.parse_args()
    try:
        if not args.apply:
            check()
        else:
            ensure_operator()
            ensure_namespace()
            ensure_console()
            owner_ns, account = ensure_manager()
            ensure_rbac(owner_ns, account)
            ensure_gitops_gateway_rbac()
            print('PASS prerequisite configuration requested. Verify plugin loading, mesh readiness and server-side admission before deploying.')
        return 0
    except (Failure, ValueError, KeyError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
