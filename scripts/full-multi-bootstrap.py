#!/usr/bin/env python3
"""Read-only fresh-cluster prerequisites, GitOps synchronization and rollout readiness.

Never creates credentials, installs controllers, promotes Rollouts, or resets data.
Only calls `oc get`; the lifecycle shell script owns the explicit `oc apply`.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from full_multi_oc import run_oc

EXPECTED_ROLLOUTS = {
    f'{tier}-{instance}' for tier in ('front', 'backend', 'crud')
    for instance in 'ab'
}


class BootstrapError(RuntimeError):
    pass


def oc_json(*args, allow_missing=False):
    try:
        raw = run_oc(*args, *(['--ignore-not-found'] if allow_missing else []), '-o', 'json')
    except subprocess.CalledProcessError as exc:
        error = exc.stderr or ''
        if allow_missing and ('NotFound' in error or 'not found' in error):
            return None
        raise BootstrapError('Command failed: %s\n%s' %
                             (' '.join(['oc', *args]), error.strip())) from exc
    try:
        if allow_missing and not raw.strip():
            return None
        return json.loads(raw)
    except ValueError as exc:
        raise BootstrapError('Invalid JSON returned by %s: %s' %
                             (' '.join(['oc', *args]), exc)) from exc


def manager_ready(manager):
    spec = manager.get('spec') or {}
    status = manager.get('status') or {}
    return (spec.get('namespaceScoped') is False and
            status.get('phase') == 'Available' and
            status.get('rolloutController') == 'Available')


def validate_secrets(items):
    problems = []
    for app in 'ab':
        item = items.get(app)
        if not item:
            problems.append(f'db-credentials-{app} is missing')
            continue
        missing = {'password', 'root-password'} - set((item.get('data') or {}).keys())
        if missing:
            problems.append(f'db-credentials-{app} lacks {", ".join(sorted(missing))}')
    return problems


def preflight(ns, argocd_ns):
    namespace = oc_json('get', 'namespace', ns, allow_missing=True)
    if namespace is None:
        raise BootstrapError(f'Namespace {ns} missing; run deploy to create it')
    labels = (namespace.get('metadata') or {}).get('labels') or {}
    if labels.get('istio-discovery') != 'enabled' or labels.get('istio.io/dataplane-mode') != 'ambient':
        raise BootstrapError(f'Namespace {ns} requires discovery and ambient labels')
    if labels.get('istio-injection') == 'enabled' or labels.get('istio.io/rev'):
        raise BootstrapError(f'Namespace {ns} still enables sidecar injection')
    if labels.get('istio.io/use-waypoint') != 'full-multi-waypoint':
        raise BootstrapError(f'Namespace {ns} requires the full-multi waypoint')

    secrets = {app: oc_json('-n', ns, 'get', 'secret', f'db-credentials-{app}',
                            allow_missing=True) for app in 'ab'}
    bad = validate_secrets(secrets)
    if bad:
        raise BootstrapError('Database Secrets prerequisite failed: ' + '; '.join(bad) +
                             '. Provision with scripts/full-multi-secrets.sh before deploy.')
    print('OK  both db-credentials-a/b Secrets exist with expected keys (values not shown)', flush=True)

    managers = oc_json('get', 'rolloutmanager', '-A').get('items') or []
    controllers = [m for m in managers if manager_ready(m)]
    if not controllers:
        summary = [f"{m['metadata']['namespace']}/{m['metadata']['name']}: "
                   f"namespaceScoped={m.get('spec', {}).get('namespaceScoped')} "
                   f"phase={m.get('status', {}).get('phase')} "
                   f"controller={m.get('status', {}).get('rolloutController')}"
                   for m in managers]
        raise BootstrapError(
            'No Available cluster-scoped RolloutManager. ' +
            ('Found: ' + '; '.join(summary) + '. ' if summary else 'No RolloutManager resources exist. ') +
            'Install explicitly using `oc apply -f platform-monitoring/rollout-manager-cluster.yaml` '
            'and verify its controller. The Argo Rollouts CRD alone is insufficient.')

    chosen = None
    for manager in controllers:
        owner_ns = manager['metadata']['namespace']
        controller = oc_json('-n', owner_ns, 'get', 'deployment', 'argo-rollouts',
                             allow_missing=True)
        if controller and (controller.get('status') or {}).get('availableReplicas', 0) >= 1:
            chosen = owner_ns
            break
    if not chosen:
        raise BootstrapError('A cluster-scoped RolloutManager is Available, but no matching '
                             'argo-rollouts Deployment reports an available controller replica.')
    oc_json('-n', argocd_ns, 'get', 'namespace', argocd_ns)
    print(f'OK  Argo Rollouts cluster-scoped controller available in {chosen}', flush=True)
    print('OK  GitOps namespace accessible', flush=True)


def check_cutover_rollouts(items, allow_empty=False):
    """Refuse to supersede any running or paused demonstration revision."""
    indexed = {x.get('metadata', {}).get('name'): x for x in items}
    if not indexed and allow_empty:
        return
    missing = sorted(EXPECTED_ROLLOUTS - set(indexed))
    if missing:
        raise BootstrapError('Cannot establish completed demo: missing Rollouts ' + ', '.join(missing))
    for name in sorted(EXPECTED_ROLLOUTS):
        obj = indexed[name]
        spec, status = obj.get('spec') or {}, obj.get('status') or {}
        generation = int(obj.get('metadata', {}).get('generation') or 1)
        observed = int(status.get('observedGeneration') or 0)
        if (status.get('phase') != 'Healthy' or observed < generation or
                spec.get('paused') or status.get('pauseConditions') or status.get('abort')):
            raise BootstrapError(f'{name} is not fully completed and healthy; finish the paused demo before ambient cutover')


def preflight_ambient_platform(namespace):
    for name in ('httproutes.gateway.networking.k8s.io', 'gateways.gateway.networking.k8s.io'):
        oc_json('get', 'crd', name)
    for name in ('istio', 'istio-waypoint'):
        oc_json('get', 'gatewayclasses.gateway.networking.k8s.io', name)
    istio = oc_json('-n', 'istio-system', 'get', 'istios.sailoperator.io', 'default')
    if (istio.get('spec') or {}).get('profile') != 'ambient':
        raise BootstrapError('Istio/default must use the ambient profile before cutover')
    ztunnels = oc_json('get', 'ztunnels.sailoperator.io', '-A').get('items') or []
    if not any(any(c.get('type') == 'Ready' and c.get('status') == 'True'
                   for c in (z.get('status') or {}).get('conditions') or []) for z in ztunnels):
        raise BootstrapError('No Ready ZTunnel resource found')
    managers = oc_json('get', 'rolloutmanager', '-A').get('items') or []
    compatible = []
    for m in managers:
        if not manager_ready(m):
            continue
        for plugin in ((m.get('spec') or {}).get('plugins') or {}).get('trafficManagement') or []:
            if (plugin.get('name') == 'argoproj-labs/gatewayAPI' and
                    '/v0.17.0/' in str(plugin.get('location') or '')):
                compatible.append(m)
    if not compatible:
        raise BootstrapError('A Ready cluster-scoped RolloutManager must configure the verified Gateway API plugin v0.17.0; see ambient runbook')
    # A configured plugin is insufficient if its controller cannot update routes.
    owner_ns = compatible[0]['metadata']['namespace']
    deployment = oc_json('-n', owner_ns, 'get', 'deployment', 'argo-rollouts')
    service_account = (((deployment.get('spec') or {}).get('template') or {}).get('spec') or {}).get('serviceAccountName')
    if not service_account:
        raise BootstrapError('Cannot identify the Rollouts controller service account')
    principal = f'system:serviceaccount:{owner_ns}:{service_account}'
    for verb, resource in [('get', 'services'), ('get', 'httproutes.gateway.networking.k8s.io'),
                           ('list', 'httproutes.gateway.networking.k8s.io'),
                           ('update', 'httproutes.gateway.networking.k8s.io'),
                           ('patch', 'httproutes.gateway.networking.k8s.io')]:
        cmd = ['oc', 'auth', 'can-i', verb, resource, '-n', namespace, '--as', principal]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode or result.stdout.strip() != 'yes':
            raise BootstrapError(f'Cannot verify {principal} can {verb} {resource} in {namespace}: '
                                 f'{result.stderr.strip() or result.stdout.strip()}')


def preflight_deploy(ns, argocd_ns, app_name):
    app = oc_json('-n', argocd_ns, 'get', 'application', app_name, allow_missing=True)
    if app is not None:
        spec = app.get('spec') or {}
        source = spec.get('source') or {}
        destination = spec.get('destination') or {}
        if (source.get('path') != 'full-multi' or
                source.get('targetRevision') != 'multi-ambient' or
                source.get('repoURL') != 'https://github.com/gstephan76/argo-sm.git' or
                destination.get('server') != 'https://kubernetes.default.svc' or
                destination.get('namespace') != ns):
            raise BootstrapError('Existing Application source or destination differs; refusing takeover')
    policy = ((app or {}).get('spec') or {}).get('syncPolicy') or {}
    automatic = policy.get('automated')
    if isinstance(automatic, dict) and automatic.get('prune') is False:
        raise BootstrapError('Existing Application has pruning disabled; refuse to re-enable it. '
                             'Complete and authorize the protected migration first.')
    namespace = oc_json('get', 'namespace', ns, allow_missing=True)
    if namespace is not None:
        items = oc_json('-n', ns, 'get', 'rollouts.argoproj.io').get('items') or []
        legacy = sorted({x.get('metadata', {}).get('name') for x in items} & {'mariadb-a', 'mariadb-b'})
        if legacy:
            raise BootstrapError(f'Legacy MariaDB Rollouts {legacy} exist; refuse pruning-enabled deployment')
        labels = (namespace.get('metadata') or {}).get('labels') or {}
        source = ((app or {}).get('spec') or {}).get('source') or {}
        migrating = (labels.get('istio.io/dataplane-mode') != 'ambient' or
                     source.get('targetRevision') not in (None, 'multi-ambient'))
        if migrating:
            check_cutover_rollouts(items, allow_empty=app is None)
            if app is not None and isinstance(automatic, dict) and automatic.get('enabled', True):
                raise BootstrapError('Disable Argo CD automatic sync before sidecar-to-ambient cutover')
    elif app is not None:
        raise BootstrapError('Existing Application has no namespace; refusing cutover')
    preflight_ambient_platform(ns)


def sync_snapshot(app, target, allow_legacy_prune_disabled=False):
    status = app.get('status') or {}
    sync = status.get('sync') or {}
    operation = status.get('operationState') or {}
    conditions = status.get('conditions') or []
    op_sync = ((operation.get('syncResult') or {}).get('revision'))
    significant = [c for c in conditions if c.get('type') in
                   ('ComparisonError', 'InvalidSpecError', 'SyncError', 'UnknownError')]
    terminal = (operation.get('phase') in ('Error', 'Failed') and op_sync == target)
    resources = status.get('resources') or []
    out_of_sync = [r for r in resources if r.get('status') == 'OutOfSync']
    protected_legacy_only = (
        allow_legacy_prune_disabled and
        (app.get('spec') or {}).get('syncPolicy', {}).get('automated', {}).get('prune') is False and
        bool(out_of_sync) and all(
            r.get('group') == 'argoproj.io' and r.get('kind') == 'Rollout' and
            r.get('name') in ('mariadb-a', 'mariadb-b') and
            r.get('namespace') == 'multi-ambient' and r.get('requiresPruning') is True
            for r in out_of_sync))
    return {
        'revision': sync.get('revision') or '',
        'sync': sync.get('status') or 'Unknown',
        'health': (status.get('health') or {}).get('status') or 'Unknown',
        'operation': operation.get('phase') or 'Unknown',
        'message': operation.get('message') or '',
        'conditions': significant,
        'done': (sync.get('revision') == target and not terminal and not significant and
                 operation.get('phase') not in ('Running', 'Terminating') and
                 (sync.get('status') == 'Synced' or
                  (sync.get('status') == 'OutOfSync' and protected_legacy_only))),
        'terminal': terminal,
    }


def wait_sync(app_name, argocd_ns, revision, timeout, poll, allow_legacy_prune_disabled=False):
    deadline = time.monotonic() + timeout
    last_message = None
    errors_seen = 0
    while True:
        app = oc_json('-n', argocd_ns, 'get', 'application', app_name)
        s = sync_snapshot(app, revision, allow_legacy_prune_disabled)
        message = ('revision=%s sync=%s health=%s operation=%s' %
                   (s['revision'][:12] or 'none', s['sync'], s['health'], s['operation']))
        if message != last_message:
            print('GITOPS ' + message, flush=True)
            last_message = message
        if s['done']:
            print(f'PASS Argo CD accepted expected revision {revision[:12]} ({s["sync"]}); '
                  'checking workload readiness separately', flush=True)
            return
        if s['terminal']:
            raise BootstrapError('Argo CD failed synchronizing requested revision: ' + s['message'])
        if s['conditions']:
            errors_seen += 1
            if errors_seen >= 2:
                raise BootstrapError('Argo CD reports persistent error conditions: ' +
                                     json.dumps(s['conditions']))
        else:
            errors_seen = 0
        if time.monotonic() >= deadline:
            raise BootstrapError(f'Argo CD did not sync revision {revision[:12]} within {timeout}s. '
                                 f'Last status: {message}. Check `oc -n {argocd_ns} '
                                 f'describe application {app_name}`.')
        time.sleep(poll)


def readiness_report(items, gateway):
    indexed = {x['metadata']['name']: x for x in items}
    missing = sorted(EXPECTED_ROLLOUTS - set(indexed))
    problems = [f'{name}: absent' for name in missing]
    degraded = []
    for name in sorted(EXPECTED_ROLLOUTS & set(indexed)):
        rollout = indexed[name]
        spec = rollout.get('spec') or {}
        status = rollout.get('status') or {}
        desired = int(spec.get('replicas', 1))
        available = int(status.get('availableReplicas') or 0)
        generation = int(rollout.get('metadata', {}).get('generation') or 1)
        observed = int(status.get('observedGeneration') or 0)
        phase = status.get('phase') or 'Unreconciled'
        if phase == 'Degraded':
            degraded.append(f'{name}: phase Degraded')
        if phase != 'Healthy' or available < desired or observed < generation:
            problems.append(f'{name}: phase={phase} available={available}/{desired} '
                            f'observedGeneration={observed}/{generation}')
    st = gateway.get('status') or {}
    gen = int(gateway.get('metadata', {}).get('generation') or 1)
    if int(st.get('observedGeneration') or 0) < gen or int(st.get('availableReplicas') or 0) < 1:
        problems.append('ingressgateway: Deployment not available')
    if any(c.get('type') == 'Progressing' and c.get('reason') == 'ProgressDeadlineExceeded'
           and c.get('status') == 'False' for c in st.get('conditions') or []):
        degraded.append('ingressgateway: ProgressDeadlineExceeded')
    return problems, degraded


def gateway_api_readiness(gateways, routes):
    problems = []
    for name in ('full-multi-gateway', 'full-multi-waypoint'):
        obj = gateways.get(name)
        if not obj:
            problems.append(f'{name}: Gateway missing')
            continue
        generation = int((obj.get('metadata') or {}).get('generation') or 1)
        conditions = (obj.get('status') or {}).get('conditions') or []
        if not any(c.get('type') == 'Programmed' and c.get('status') == 'True' and
                   int(c.get('observedGeneration') or 0) >= generation for c in conditions):
            problems.append(f'{name}: Gateway not Programmed for current generation')
    names = ['full-multi-front-router'] + [f'{tier}-{app}-router'
             for tier in ('backend', 'crud') for app in 'ab']
    for name in names:
        obj = routes.get(name)
        if not obj:
            problems.append(f'{name}: HTTPRoute missing')
            continue
        generation = int((obj.get('metadata') or {}).get('generation') or 1)
        parents = (obj.get('status') or {}).get('parents') or []
        def ready(parent):
            conditions = parent.get('conditions') or []
            return all(any(c.get('type') == kind and c.get('status') == 'True' and
                           int(c.get('observedGeneration') or 0) >= generation
                           for c in conditions) for kind in ('Accepted', 'ResolvedRefs'))
        if not any(ready(parent) for parent in parents):
            problems.append(f'{name}: HTTPRoute not accepted or references unresolved')
    return problems


def wait_ready(ns, timeout, poll):
    deadline = time.monotonic() + timeout
    last_report = None
    while True:
        items = oc_json('-n', ns, 'get', 'rollouts.argoproj.io').get('items') or []
        gateway = oc_json('-n', ns, 'get', 'deployment', 'full-multi-gateway-istio',
                          allow_missing=True)
        if gateway is None:
            pending, degraded = ['ingressgateway: Deployment missing'], []
        else:
            pending, degraded = readiness_report(items, gateway)
        databases = {item['metadata']['name']: item for item in
                     (oc_json('-n', ns, 'get', 'statefulsets.apps').get('items') or [])}
        for app in 'ab':
            for track in ('blue', 'green'):
                name = f'mariadb-{app}-{track}'
                db = databases.get(name)
                if db is None or (db.get('status') or {}).get('readyReplicas', 0) < 1:
                    pending.append(f'{name}: StatefulSet not Ready')
        pvcs = oc_json('-n', ns, 'get', 'persistentvolumeclaims').get('items') or []
        for app in 'ab':
            for track in ('blue', 'green'):
                name = f'mariadb-data-mariadb-{app}-{track}-0'
                pvc = next((x for x in pvcs if x['metadata']['name'] == name), None)
                if not pvc or (pvc.get('status') or {}).get('phase') != 'Bound':
                    pending.append(f'{name}: PVC not Bound')
        summary = '; '.join(pending) if pending else 'all six Rollouts, four databases and ingressgateway available'
        if summary != last_report:
            print('READY  ' + summary, flush=True)
            last_report = summary
        if pending:
            # Detect admission failures before the Deployment progress deadline.
            # Ignore obsolete scaled-down ReplicaSets, which can retain old
            # FailedCreate events after a successful GitOps correction.
            replicasets = oc_json('-n', ns, 'get', 'replicasets').get('items') or []
            for rs in replicasets:
                if int((rs.get('spec') or {}).get('replicas') or 0) < 1:
                    continue
                for condition in (rs.get('status') or {}).get('conditions') or []:
                    if condition.get('status') == 'True' and condition.get('reason') == 'FailedCreate':
                        degraded.append('%s: FailedCreate: %s' % (
                            rs['metadata']['name'], condition.get('message', '')))
        if degraded:
            raise BootstrapError('Unhealthy resources: ' + '; '.join(degraded) +
                                 f'. Inspect `oc -n {ns} get events --sort-by=.lastTimestamp` '
                                 'and `oc describe` for the affected Rollout/Deployment.')
        if not pending:
            gateways = {x['metadata']['name']: x for x in
                        (oc_json('-n', ns, 'get', 'gateways.gateway.networking.k8s.io').get('items') or [])}
            routes = {x['metadata']['name']: x for x in
                      (oc_json('-n', ns, 'get', 'httproutes.gateway.networking.k8s.io').get('items') or [])}
            pending.extend(gateway_api_readiness(gateways, routes))
        if not pending:
            route = oc_json('-n', ns, 'get', 'route', 'multi-ambient')
            host = (route.get('spec') or {}).get('host')
            if not host:
                raise BootstrapError('Route/multi-ambient has no host')
            print(f'PASS all six Rollouts Healthy, gateway available, route=https://{host}', flush=True)
            print('INFO Tempo ingestion and Kiali trace visibility require separate end-to-end checks.', flush=True)
            return
        if time.monotonic() >= deadline:
            raise BootstrapError(f'Workloads not ready within {timeout}s: {summary}. '
                                 f'Inspect `oc -n {ns} get pods,rollouts` and events.')
        time.sleep(poll)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['preflight', 'preflight-deploy', 'wait-sync', 'wait-ready'])
    p.add_argument('--namespace', default='multi-ambient')
    p.add_argument('--argocd-namespace', default='openshift-gitops')
    p.add_argument('--application', default='multi-ambient')
    p.add_argument('--revision')
    p.add_argument('--timeout', type=int, default=900)
    p.add_argument('--poll', type=int, default=5)
    p.add_argument('--allow-legacy-prune-disabled', action='store_true',
                   help='Allow only the two obsolete MariaDB Rollouts to require pruning')
    x = p.parse_args()
    if x.timeout < 1 or x.poll < 1:
        p.error('--timeout and --poll must be positive')
    if x.command == 'wait-sync' and not x.revision:
        p.error('wait-sync requires --revision')
    try:
        if x.command == 'preflight':
            preflight(x.namespace, x.argocd_namespace)
        elif x.command == 'preflight-deploy':
            preflight_deploy(x.namespace, x.argocd_namespace, x.application)
        elif x.command == 'wait-sync':
            wait_sync(x.application, x.argocd_namespace, x.revision, x.timeout, x.poll,
                      x.allow_legacy_prune_disabled)
        else:
            wait_ready(x.namespace, x.timeout, x.poll)
        return 0
    except (BootstrapError, subprocess.TimeoutExpired, ValueError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
