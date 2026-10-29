#!/usr/bin/env python3
"""Fail-closed opt-in k8s.namespace.name enrichment for full-multi traces.

No full-multi workload, VirtualService, Argo CD Application, or TempoStack is
modified. The shared istio-system/otel Collector CR is patched only on --apply.
All writes are explicit; --check and --verify are read-only.
"""
import argparse
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
RBAC_FILE = ROOT / 'platform-monitoring' / 'otel-full-multi-rbac.yaml'
NAMESPACE = 'full-multi'
COLLECTOR_NS = 'istio-system'
COLLECTOR_NAME = 'otel'
SERVICE_ACCOUNT = 'otel-collector'
LABEL_VALUE = 'full-multi-otel-enrichment'
OLD_PROCESSORS = ['memory_limiter', 'batch']
NEW_PROCESSORS = ['memory_limiter', 'k8sattributes', 'batch']
PROCESSOR = {
    'auth_type': 'serviceAccount',
    'filter': {'namespace': NAMESPACE},
    'passthrough': False,
    'extract': {'metadata': [
        'k8s.namespace.name', 'k8s.pod.name', 'k8s.pod.uid'
    ]},
    'pod_association': [
        {'sources': [{'from': 'resource_attribute', 'name': 'k8s.pod.ip'}]},
        {'sources': [{'from': 'connection'}]},
    ],
}


def run(*args, input_text=None):
    proc = subprocess.run(['oc', *args], input=input_text, text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=45)
    if proc.returncode:
        raise RuntimeError('oc %s failed (%d): %s' %
                           (' '.join(args[:7]), proc.returncode, proc.stderr.strip()))
    return proc.stdout


def read_resource(kind, name, namespace):
    value = run('-n', namespace, 'get', kind, name, '-o', 'json',
                '--ignore-not-found').strip()
    return json.loads(value) if value else None


def collector_snapshot():
    cr = read_resource('opentelemetrycollector', COLLECTOR_NAME, COLLECTOR_NS)
    if not cr:
        raise RuntimeError('Missing OpenTelemetryCollector istio-system/otel')
    if cr.get('spec', {}).get('serviceAccount') != SERVICE_ACCOUNT:
        raise RuntimeError('Collector serviceAccount changed; refusing')
    config = cr.get('spec', {}).get('config')
    if not isinstance(config, dict):
        raise RuntimeError('Collector spec.config is not structured JSON')
    traces = config.get('service', {}).get('pipelines', {}).get('traces', {})
    if traces.get('receivers') != ['otlp'] or traces.get('exporters') != ['otlp_grpc/tempo']:
        raise RuntimeError('Unexpected receiver/exporter: refusing to modify pipeline')
    tempo = config.get('exporters', {}).get('otlp_grpc/tempo', {})
    if tempo.get('endpoint') != 'tempo-mesh-gateway.tempo.svc.cluster.local:8090':
        raise RuntimeError('Tempo endpoint changed; refusing')
    proc = config.get('processors', {})
    current = traces.get('processors')
    if current == OLD_PROCESSORS and 'k8sattributes' not in proc:
        state = 'baseline'
    elif current == NEW_PROCESSORS and proc.get('k8sattributes') == PROCESSOR:
        state = 'enriched'
    else:
        raise RuntimeError('Unrecognized Collector processor configuration; refusing to overwrite it: %r' % current)
    return cr, state


def check_rbac():
    role = read_resource('role', 'otel-full-multi-reader', NAMESPACE)
    binding = read_resource('rolebinding', 'otel-full-multi-reader', NAMESPACE)
    for kind, item in [('Role', role), ('RoleBinding', binding)]:
        if item and item.get('metadata', {}).get('labels', {}).get('app.kubernetes.io/managed-by') != LABEL_VALUE:
            raise RuntimeError('%s/full-multi/otel-full-multi-reader exists but is not managed by this patch' % kind)
    if role and role.get('rules') != [{
        'apiGroups': [''], 'resources': ['pods'], 'verbs': ['get', 'list', 'watch']
    }]:
        raise RuntimeError('Role has unexpected permissions; refusing')
    if binding:
        if binding.get('roleRef') != {
            'apiGroup': 'rbac.authorization.k8s.io', 'kind': 'Role',
            'name': 'otel-full-multi-reader'
        } or binding.get('subjects') != [{
            'kind': 'ServiceAccount', 'name': SERVICE_ACCOUNT, 'namespace': COLLECTOR_NS
        }]:
            raise RuntimeError('RoleBinding has unexpected subject or roleRef; refusing')
    return bool(role), bool(binding)


def json_patch(cr, apply):
    rv = cr['metadata']['resourceVersion']
    changes = [
        {'op': 'test', 'path': '/metadata/resourceVersion', 'value': rv},
        {'op': 'test', 'path': '/spec/config/service/pipelines/traces/processors',
         'value': OLD_PROCESSORS if apply else NEW_PROCESSORS},
    ]
    if apply:
        changes += [
            {'op': 'add', 'path': '/spec/config/processors/k8sattributes', 'value': PROCESSOR},
            {'op': 'add', 'path': '/spec/config/service/pipelines/traces/processors/1',
             'value': 'k8sattributes'},
        ]
    else:
        changes += [
            {'op': 'test', 'path': '/spec/config/processors/k8sattributes', 'value': PROCESSOR},
            {'op': 'remove', 'path': '/spec/config/service/pipelines/traces/processors/1'},
            {'op': 'remove', 'path': '/spec/config/processors/k8sattributes'},
        ]
    return json.dumps(changes, separators=(',', ':'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--check', action='store_true', help='Read-only preflight; no mutation')
    mode.add_argument('--apply', action='store_true', help='Add namespace RBAC and enable enrichment')
    mode.add_argument('--verify', action='store_true', help='Read-only status (not a Tempo ingestion test)')
    mode.add_argument('--rollback', action='store_true', help='Disable enrichment; leave RBAC in place')
    args = parser.parse_args()
    if not RBAC_FILE.is_file():
        parser.error('Run from the repository with platform-monitoring/otel-full-multi-rbac.yaml')
    try:
        cr, state = collector_snapshot()
        role, binding = check_rbac()
        print('Collector: %s/%s; generation=%s; config=%s; Role=%s; RoleBinding=%s' %
              (COLLECTOR_NS, COLLECTOR_NAME, cr['metadata'].get('generation'),
               state, role, binding))
        print('Target trace processor chain:', ' -> '.join(NEW_PROCESSORS))
        print('Pod association: k8s.pod.ip resource attribute, then OTLP connection IP')
        if args.check:
            print('PASS: read-only preflight; would %s' %
                  ('enable k8sattributes' if state == 'baseline' else 'make no Collector change'))
            return 0
        if args.verify:
            if state != 'enriched' or not (role and binding):
                raise RuntimeError('Enrichment is not fully installed')
            print('PASS: Collector and RBAC configuration installed. Verify NEW trace resource attributes in Tempo.')
            return 0
        if args.apply:
            if state == 'enriched' and role and binding:
                print('PASS: already installed; no changes')
                return 0
            # Create/refresh only resources with exact expected permissions and ownership.
            # Existing conflicting resources were rejected above.
            run('apply', '-f', str(RBAC_FILE))
            r2, b2 = check_rbac()
            if not (r2 and b2):
                raise RuntimeError('RBAC apply did not create both resources')
            if state == 'baseline':
                # Fails closed if CR resourceVersion or processor list changed meanwhile.
                run('-n', COLLECTOR_NS, 'patch', 'opentelemetrycollector', COLLECTOR_NAME,
                    '--type=json', '-p', json_patch(cr, True))
            fresh, s2 = collector_snapshot()
            if s2 != 'enriched':
                raise RuntimeError('Collector failed to reach expected spec')
            print('PASS: requested Collector enrichment; validate operator reconciliation and NEW Tempo spans')
            return 0
        if args.rollback:
            if state == 'baseline':
                print('PASS: Collector already at baseline; RBAC not changed')
                return 0
            run('-n', COLLECTOR_NS, 'patch', 'opentelemetrycollector', COLLECTOR_NAME,
                '--type=json', '-p', json_patch(cr, False))
            _, s2 = collector_snapshot()
            if s2 != 'baseline':
                raise RuntimeError('Collector rollback did not restore baseline spec')
            print('PASS: Collector processor rolled back. RBAC deliberately left intact; remove separately if no longer needed.')
            return 0
        return 2
    except (RuntimeError, subprocess.TimeoutExpired, ValueError, KeyError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
