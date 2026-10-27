#!/usr/bin/env python3
"""Preview or deliberately configure a shared Kiali instance for existing TempoStack.

Default: read-only plan. --apply performs a merge patch only in Kiali's
spec.external_services.tracing; no other Kiali settings are replaced.
This is a platform-level operation, not part of full-multi Argo CD sync.
"""
import argparse
import json
import re
import subprocess
import sys


def oc(*args):
    p = subprocess.run(['oc', *args], capture_output=True, text=True, timeout=25)
    if p.returncode:
        raise RuntimeError('oc %s: %s' % (' '.join(args), p.stderr.strip()))
    return p.stdout


def get(kind, name, namespace):
    return json.loads(oc('get', kind, name, '-n', namespace, '-o', 'json'))


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--tenant', required=True, help='Tempo tenant NAME confirmed in TempoStack (no automatic guess)')
    ap.add_argument('--kiali-name', default='kiali')
    ap.add_argument('--kiali-namespace', default='istio-system')
    ap.add_argument('--tempo-name', default='mesh')
    ap.add_argument('--tempo-namespace', default='tempo')
    ap.add_argument('--apply', action='store_true', help='Explicitly mutate existing Kiali CR')
    ap.add_argument('--replace-existing', action='store_true', help='Permit replacement of an existing divergent tracing configuration')
    x = ap.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,62}', x.tenant):
        ap.error('invalid tenant; use a simple Tempo tenant name')
    try:
        cr = get('kialis.kiali.io', x.kiali_name, x.kiali_namespace)
        stack = get('tempostacks.tempo.grafana.com', x.tempo_name, x.tempo_namespace)
        route = get('route', 'console', 'openshift-console')
        console_host = route.get('spec', {}).get('host')
        if not console_host: raise RuntimeError('OpenShift Console Route has no hostname')
        gateway_service = 'tempo-%s-gateway' % x.tempo_name
        _ = get('service', gateway_service, x.tempo_namespace)
        tenant_mode = (stack.get('spec', {}).get('tenants') or {}).get('mode')
        declared = [item.get('tenantName') for item in ((stack.get('spec', {}).get('tenants') or {}).get('authentication') or []) if item.get('tenantName')]
        if declared and x.tenant not in declared:
            raise RuntimeError('Tenant %r is not listed in TempoStack authentication: %s' % (x.tenant, declared))
        base = 'https://%s.%s.svc.cluster.local:8080' % (gateway_service, x.tempo_namespace)
        api = base + '/api/traces/v1/' + x.tenant + '/tempo'
        tracing = {
            'enabled': True,
            'provider': 'tempo',
            'use_grpc': False,
            'internal_url': api,
            'health_check_url': api + '/api/echo',
            'external_url': 'https://' + console_host,
            'auth': {'type': 'bearer', 'use_kiali_token': True, 'insecure_skip_verify': False},
            'tempo_config': {'url_format': 'openshift', 'name': x.tempo_name, 'namespace': x.tempo_namespace, 'tenant': x.tenant},
        }
        current = ((cr.get('spec') or {}).get('external_services') or {}).get('tracing') or {}
        changed = [key for key in tracing if key in current and current[key] != tracing[key]]
        patch = {'spec': {'external_services': {'tracing': tracing}}}
        print('Kiali: %s/%s; TempoStack: %s/%s; tenants.mode=%s; declared tenants=%s' %
              (x.kiali_namespace, x.kiali_name, x.tempo_namespace, x.tempo_name, tenant_mode, declared))
        print('Current tracing fields that differ:', ', '.join(changed) or 'none')
        print('Proposed JSON merge patch (contains no credentials):')
        print(json.dumps(patch, indent=2, sort_keys=True))
        if not x.apply:
            print('DRY RUN: no changes applied. To apply, pass --apply (and --replace-existing when intentionally replacing current settings).')
            return 0
        if changed and not x.replace_existing:
            raise RuntimeError('Kiali tracing is already configured differently. Refusing to overwrite. Inspect and retry with --replace-existing if intended.')
        # Check operator ownership: this will be reconciled by its owner; commit
        # equivalent setting to the Kiali installation's own GitOps source.
        oc('patch', 'kialis.kiali.io', x.kiali_name, '-n', x.kiali_namespace,
           '--type=merge', '-p', json.dumps(patch))
        print('PASS: Kiali tracing patch submitted. Verify Kiali CR and service logs; update its platform GitOps source if owned there.')
        return 0
    except (RuntimeError, subprocess.TimeoutExpired, ValueError) as e:
        print('ERROR:', e, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
