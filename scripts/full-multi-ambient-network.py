#!/usr/bin/env python3
"""Check or enable OVN local gateway mode for ambient health probes.

Default is read-only; --apply changes the cluster-wide OVN gateway configuration.
A retry preserves all other network settings. It waits for the network operator,
OVN configuration and daemonset to reach a stable ready state.
"""
import argparse
import configparser
import json
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
from full_multi_oc import run_oc


class Failure(RuntimeError):
    pass


def oc(*args):
    try:
        return run_oc(*args)
    except subprocess.CalledProcessError as exc:
        raise Failure(f'oc {" ".join(args[:5])} failed: {(exc.stderr or "").strip()}') from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f'oc failed: {exc}') from exc


def read(kind, name, namespace=None):
    args = ['get', kind, name]
    if namespace:
        args += ['-n', namespace]
    return json.loads(oc(*args, '-o', 'json'))


def gateway_setting(network):
    spec = network.get('spec') or {}
    default = spec.get('defaultNetwork') or {}
    if default.get('type') != 'OVNKubernetes':
        raise Failure('Cluster is not configured for OVNKubernetes')
    gateway = ((default.get('ovnKubernetesConfig') or {}).get('gatewayConfig') or {})
    value = gateway.get('routingViaHost')
    if value is not True and value is not False:
        raise Failure('routingViaHost is absent or invalid; inspect the network configuration')
    return value


def ensure_gateway(network):
    if gateway_setting(network):
        print('OVN gateway already requested; verifying reconciliation', flush=True)
        return
    # Status-only updates must not invalidate this transaction. Guard the
    # inspected configuration and object identity instead of resourceVersion.
    operations = [{'op': 'test', 'path': '/metadata/uid', 'value': network['metadata']['uid']},
                  {'op': 'test', 'path': '/spec', 'value': network['spec']},
                  {'op': 'replace', 'path': '/spec/defaultNetwork/ovnKubernetesConfig/gatewayConfig/routingViaHost',
                   'value': True}]
    try:
        oc('patch', 'networks.operator.openshift.io', 'cluster', '--type=json', '-p', json.dumps(operations))
    except Failure:
        latest = read('networks.operator.openshift.io', 'cluster')
        if latest['metadata']['uid'] != network['metadata']['uid'] or not gateway_setting(latest):
            raise
        print('OVN gateway request already applied; verifying reconciliation', flush=True)
    else:
        print('UPDATED cluster OVN gateway mode; waiting for reconciliation', flush=True)


def ready(network, operator, daemonset, configmap):
    if not gateway_setting(network):
        return False
    conditions = {c.get('type'): c.get('status') for c in operator.get('status', {}).get('conditions', [])}
    if any(conditions.get(k) != v for k, v in [('Available', 'True'), ('Progressing', 'False'), ('Degraded', 'False')]):
        return False
    data = configmap.get('data') or {}
    found = False
    for content in data.values():
        parser = configparser.ConfigParser(interpolation=None, strict=False)
        try:
            parser.read_string(content)
        except configparser.Error:
            continue
        if parser.has_section('gateway') and parser.get('gateway', 'mode', fallback='').strip() == 'local':
            found = True
            break
    if not found:
        return False
    metadata, status = daemonset.get('metadata') or {}, daemonset.get('status') or {}
    desired = status.get('desiredNumberScheduled') or 0
    return (desired > 0 and status.get('observedGeneration', 0) >= metadata.get('generation', 1) and
            status.get('updatedNumberScheduled', 0) == desired and status.get('numberReady', 0) == desired and
            status.get('numberAvailable', 0) == desired and status.get('numberMisscheduled', 0) == 0)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--check', action='store_true', help='Read-only inspection (default)')
    mode.add_argument('--apply', action='store_true', help='Enable local gateway mode cluster-wide')
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--poll', type=int, default=10)
    args = parser.parse_args()
    if args.timeout < 60 or args.poll < 1:
        parser.error('--timeout must be at least 60 seconds and --poll at least 1 second')
    try:
        network = read('networks.operator.openshift.io', 'cluster')
        before = gateway_setting(network)
        print(f'OVN routingViaHost={str(before).lower()}', flush=True)
        if not args.apply:
            print('Read-only check; no change made.', flush=True)
            return 0
        ensure_gateway(network)
        deadline, stable_since = time.monotonic() + args.timeout, None
        while time.monotonic() < deadline:
            network = read('networks.operator.openshift.io', 'cluster')
            operator = read('clusteroperator', 'network')
            daemonset = read('daemonset', 'ovnkube-node', 'openshift-ovn-kubernetes')
            configmap = read('configmap', 'ovnkube-config', 'openshift-ovn-kubernetes')
            if ready(network, operator, daemonset, configmap):
                stable_since = stable_since or time.monotonic()
                if time.monotonic() - stable_since >= 20:
                    print('PASS local gateway requested, OVN config local and network/daemonset stable', flush=True)
                    return 0
            else:
                stable_since = None
            time.sleep(args.poll)
        raise Failure('Timed out waiting for network reconciliation; inspect cluster operator and OVN before retrying')
    except (Failure, ValueError, KeyError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
