#!/usr/bin/env python3
"""Read-only fresh-cluster prerequisites, GitOps synchronization and rollout readiness.

Never creates credentials, installs controllers, promotes Rollouts, or resets data.
Only calls `oc get`; the lifecycle shell script owns the explicit `oc apply`.
"""
import argparse
import json
import subprocess
import sys
import time

EXPECTED_ROLLOUTS = {
    f'{tier}-{instance}' for tier in ('front', 'backend', 'crud')
    for instance in 'ab'
}


class BootstrapError(RuntimeError):
    pass


def oc_json(*args, allow_missing=False):
    cmd = ['oc', *args, '-o', 'json']
    p = subprocess.run(cmd, text=True, capture_output=True, timeout=30)
    if p.returncode:
        if allow_missing and ('NotFound' in p.stderr or 'not found' in p.stderr):
            return None
        raise BootstrapError('Command failed: %s\n%s' %
                             (' '.join(cmd), p.stderr.strip()))
    try:
        return json.loads(p.stdout)
    except ValueError as exc:
        raise BootstrapError('Invalid JSON returned by %s: %s' %
                             (' '.join(cmd), exc)) from exc


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
    for key in ('istio-discovery', 'istio-injection'):
        if labels.get(key) != 'enabled':
            raise BootstrapError(f'Namespace {ns} requires label {key}=enabled')

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


def sync_snapshot(app, target):
    status = app.get('status') or {}
    sync = status.get('sync') or {}
    operation = status.get('operationState') or {}
    conditions = status.get('conditions') or []
    op_sync = ((operation.get('syncResult') or {}).get('revision'))
    significant = [c for c in conditions if c.get('type') in
                   ('ComparisonError', 'InvalidSpecError', 'SyncError', 'UnknownError')]
    terminal = (operation.get('phase') in ('Error', 'Failed') and op_sync == target)
    return {
        'revision': sync.get('revision') or '',
        'sync': sync.get('status') or 'Unknown',
        'health': (status.get('health') or {}).get('status') or 'Unknown',
        'operation': operation.get('phase') or 'Unknown',
        'message': operation.get('message') or '',
        'conditions': significant,
        'done': sync.get('revision') == target and sync.get('status') == 'Synced',
        'terminal': terminal,
    }


def wait_sync(app_name, argocd_ns, revision, timeout, poll):
    deadline = time.monotonic() + timeout
    last_message = None
    errors_seen = 0
    while True:
        app = oc_json('-n', argocd_ns, 'get', 'application', app_name)
        s = sync_snapshot(app, revision)
        message = ('revision=%s sync=%s health=%s operation=%s' %
                   (s['revision'][:12] or 'none', s['sync'], s['health'], s['operation']))
        if message != last_message:
            print('GITOPS ' + message, flush=True)
            last_message = message
        if s['done']:
            print(f'PASS Argo CD synced expected revision {revision[:12]}; '
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


def wait_ready(ns, timeout, poll):
    deadline = time.monotonic() + timeout
    last_report = None
    while True:
        items = oc_json('-n', ns, 'get', 'rollouts.argoproj.io').get('items') or []
        gateway = oc_json('-n', ns, 'get', 'deployment', 'full-multi-ingressgateway',
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
            route = oc_json('-n', ns, 'get', 'route', 'full-multi')
            host = (route.get('spec') or {}).get('host')
            if not host:
                raise BootstrapError('Route/full-multi has no host')
            print(f'PASS all six Rollouts Healthy, gateway available, route=https://{host}', flush=True)
            print('INFO Tempo ingestion and Kiali trace visibility require separate end-to-end checks.', flush=True)
            return
        if time.monotonic() >= deadline:
            raise BootstrapError(f'Workloads not ready within {timeout}s: {summary}. '
                                 f'Inspect `oc -n {ns} get pods,rollouts` and events.')
        time.sleep(poll)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('command', choices=['preflight', 'wait-sync', 'wait-ready'])
    p.add_argument('--namespace', default='full-multi')
    p.add_argument('--argocd-namespace', default='openshift-gitops')
    p.add_argument('--application', default='full-multi')
    p.add_argument('--revision')
    p.add_argument('--timeout', type=int, default=900)
    p.add_argument('--poll', type=int, default=5)
    x = p.parse_args()
    if x.timeout < 1 or x.poll < 1:
        p.error('--timeout and --poll must be positive')
    if x.command == 'wait-sync' and not x.revision:
        p.error('wait-sync requires --revision')
    try:
        if x.command == 'preflight':
            preflight(x.namespace, x.argocd_namespace)
        elif x.command == 'wait-sync':
            wait_sync(x.application, x.argocd_namespace, x.revision, x.timeout, x.poll)
        else:
            wait_ready(x.namespace, x.timeout, x.poll)
        return 0
    except (BootstrapError, subprocess.TimeoutExpired, ValueError) as exc:
        print('ERROR: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
