#!/usr/bin/env python3
"""Terminate the known Gateway-RBAC-blocked sync so Argo CD can retry it.

Read-only unless --apply is set. Refuses other revisions, sources, failures,
operations, or missing Gateway permission. Does not modify the desired spec.
"""
import argparse
import json
import subprocess
import sys


class Failure(RuntimeError):
    pass


def oc(*args):
    result = subprocess.run(['oc', *args], text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise Failure(f'oc {" ".join(args[:5])}: {result.stderr.strip()}')
    return result.stdout.strip()


def validate(app, revision):
    source = (app.get('spec') or {}).get('source') or {}
    destination = (app.get('spec') or {}).get('destination') or {}
    if (source.get('repoURL') != 'https://github.com/gstephan76/argo-sm.git' or
            source.get('path') != 'full-multi' or source.get('targetRevision') != 'multi-ambient' or
            destination.get('namespace') != 'multi-ambient'):
        raise Failure('Unexpected Application source or destination')
    operation = app.get('operation') or {}
    state = (app.get('status') or {}).get('operationState') or {}
    sync_result = state.get('syncResult') or {}
    phase = state.get('phase')
    if operation.get('sync', {}).get('revision') == revision and phase == 'Terminating':
        return 'waiting'
    if operation.get('sync', {}).get('revision') == revision and operation.get('initiatedBy', {}).get('username') == 'full-multi-gateway-retry':
        return 'waiting'
    errors = [x for x in sync_result.get('resources', []) if x.get('status') == 'SyncFailed']
    if ({x.get('name') for x in errors} != {'full-multi-gateway', 'full-multi-waypoint'} or
            len(errors) != 2 or any(x.get('kind') != 'Gateway' or
                'is forbidden' not in x.get('message', '') or 'cannot create resource "gateways"' not in x.get('message', '')
                for x in errors)):
        raise Failure('Sync errors are not exactly the recognized Gateway permission failures')
    if sync_result.get('revision') != revision:
        raise Failure('Unexpected recorded sync revision')
    if phase == 'Running' and operation.get('initiatedBy', {}).get('automated') and operation.get('sync', {}).get('revision') == revision:
        return 'terminate'
    if phase == 'Failed' and state.get('message') == 'Operation terminated' and not operation:
        return 'resync'
    raise Failure('Unexpected operation; refusing to retry')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    try:
        app = json.loads(oc('-n', 'openshift-gitops', 'get', 'application', 'multi-ambient', '-o', 'json'))
        action = validate(app, args.revision)
        if action == 'waiting':
            print('PASS operation is already terminating or retrying')
            return 0
        allowed = oc('auth', 'can-i', 'create', 'gateways.gateway.networking.k8s.io', '-n', 'multi-ambient',
                     '--as=system:serviceaccount:openshift-gitops:openshift-gitops-argocd-application-controller')
        if allowed != 'yes':
            raise Failure('GitOps controller still cannot create Gateways')
        if not args.apply:
            print(f'CHECK: recognized {action} stage and verified Gateway create permission; no changes made')
            return 0
        patch = [{'op': 'test', 'path': '/metadata/resourceVersion', 'value': app['metadata']['resourceVersion']}]
        if action == 'terminate':
            patch += [{'op': 'test', 'path': '/status/operationState/phase', 'value': 'Running'},
                      {'op': 'test', 'path': '/operation/sync/revision', 'value': args.revision},
                      {'op': 'replace', 'path': '/status/operationState/phase', 'value': 'Terminating'}]
        else:
            policy = (app.get('spec') or {}).get('syncPolicy') or {}
            if (policy.get('automated') or {}).get('prune') is not True:
                raise Failure('Unexpected pruning policy; refusing resync')
            patch += [{'op': 'test', 'path': '/status/operationState/phase', 'value': 'Failed'},
                      {'op': 'test', 'path': '/status/operationState/syncResult/revision', 'value': args.revision},
                      {'op': 'add', 'path': '/operation', 'value': {
                          'initiatedBy': {'username': 'full-multi-gateway-retry'},
                          'sync': {'revision': args.revision, 'prune': True,
                                   'syncOptions': policy.get('syncOptions') or [], 'syncStrategy': {'hook': {}}}}}]
        oc('-n', 'openshift-gitops', 'patch', 'application', 'multi-ambient', '--type=json', '-p', json.dumps(patch))
        print(f'PASS requested {action} of the recognized Gateway permission sync')
        return 0
    except (Failure, KeyError, ValueError, OSError, subprocess.TimeoutExpired) as exc:
        print('ERROR:', exc, file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
