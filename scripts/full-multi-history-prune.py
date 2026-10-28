#!/usr/bin/env python3
"""Delete only obsolete, completed Argo Rollouts history without disturbing a live update.

An obsolete ReplicaSet is owned by the selected Rollout, scaled to zero,
and neither the stable nor current template hash. Completed AnalysisRuns
associated with that retired hash may be removed as well. Active runs,
active Service selectors, and protected ReplicaSets are never touched.
"""
import argparse
import json
import re
import subprocess
import sys
from datetime import datetime, timezone

NS = 'full-multi'
FINAL = {'Successful', 'Failed', 'Inconclusive', 'Error'}
TIERS = ('mariadb', 'backend', 'front', 'crud')


def oc(*args):
    return subprocess.check_output(
        ['oc', '--request-timeout=12s', *args], text=True, timeout=35
    )


def get(kind, name=None):
    return json.loads(oc('get', kind, *([name] if name else []), '-n', NS, '-o', 'json'))


def owned_by(obj, name, uid):
    return any(ref.get('kind') == 'Rollout' and ref.get('name') == name
               and ref.get('uid') == uid
               for ref in obj.get('metadata', {}).get('ownerReferences', []))


def hash_from_rs(obj, rollout_name):
    name = obj['metadata']['name']
    prefix = rollout_name + '-'
    if not name.startswith(prefix):
        return None
    hash_value = name[len(prefix):]
    return hash_value if re.fullmatch(r'[a-z0-9]+', hash_value) else None


def hash_from_analysis(obj, rollout_name):
    name = obj['metadata']['name']
    prefix = rollout_name + '-'
    if not name.startswith(prefix):
        return None
    # Inline analysis run format: <rollout>-<hash>-<revision>-<step>[.retry].
    m = re.fullmatch(r'([a-z0-9]+)-\d+-\d+(?:\.\d+)?', name[len(prefix):])
    return m.group(1) if m else None


def running(rs):
    spec = rs.get('spec', {})
    status = rs.get('status', {})
    return any((spec.get('replicas', 0), status.get('replicas', 0),
                status.get('readyReplicas', 0), status.get('availableReplicas', 0)))


def still_in_scale_down_delay(rs):
    stamp = rs.get('metadata', {}).get('annotations', {}).get(
        'argo-rollouts.argoproj.io/scale-down-deadline'
    )
    if not stamp:
        return False
    try:
        deadline = datetime.fromisoformat(stamp.replace('Z', '+00:00'))
        return deadline > datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return True  # Unrecognized deadline format: fail closed.


def select_stale(rollout, replica_sets, analysis_runs, services):
    name = rollout['metadata']['name']
    uid = rollout['metadata']['uid']
    status = rollout.get('status', {})
    stable = status.get('stableRS')
    current = status.get('currentPodHash')
    if not stable or not current:
        raise RuntimeError(f'{name}: stableRS/currentPodHash missing; refusing cleanup')

    rss = [o for o in replica_sets if owned_by(o, name, uid)]
    ars = [o for o in analysis_runs if owned_by(o, name, uid)]
    protected = {stable, current}
    for rs in rss:
        h = hash_from_rs(rs, name)
        if h and (running(rs) or rs['metadata'].get('deletionTimestamp')
                  or still_in_scale_down_delay(rs)):
            protected.add(h)
    # Do not delete a revision that is still the destination of any Service.
    for service in services:
        selector = service.get('spec', {}).get('selector') or {}
        if selector.get('app') == name:
            h = selector.get('rollouts-pod-template-hash')
            if h:
                protected.add(h)
    # Do not delete an old RS if analysis against that revision is still running.
    for ar in ars:
        h = hash_from_analysis(ar, name)
        if h and ar.get('status', {}).get('phase') not in FINAL:
            protected.add(h)

    old_rs = [o for o in rss if hash_from_rs(o, name)
              and hash_from_rs(o, name) not in protected and not running(o)]
    old_hashes = {hash_from_rs(o, name) for o in old_rs}
    # A completed run can outlive its RS. It is still obsolete unless its hash
    # belongs to a protected revision, or an unrecognized naming convention.
    known_hashes = {hash_from_rs(o, name) for o in rss}
    old_ar = [o for o in ars if o.get('status', {}).get('phase') in FINAL
              and (h := hash_from_analysis(o, name)) is not None
              and h not in protected
              and (h in old_hashes or h not in known_hashes)]
    return old_rs, old_ar, (stable, current)


def cleanup_one(tier, app, dry_run):
    name = f'{tier}-{app}'
    def snapshot():
        rollout = get('rollouts.argoproj.io', name)
        rss = get('replicasets.apps')['items']
        ars = get('analysisruns.argoproj.io')['items']
        svcs = get('services')['items']
        return rollout, rss, ars, svcs

    rollout, rss, ars, svcs = snapshot()
    if not rollout.get('status', {}).get('stableRS') or not rollout.get('status', {}).get('currentPodHash'):
        print(f'{name}: SKIP (Rollout has no stable/current hash yet)', flush=True)
        return
    uid = rollout['metadata']['uid']
    stale_rs, stale_ar, hashes = select_stale(rollout, rss, ars, svcs)
    print(f'{name}: stable={hashes[0]}, current={hashes[1]}, '
          f'obsolete RS={len(stale_rs)}, completed old AR={len(stale_ar)}', flush=True)
    for obj in stale_ar:
        print(('WOULD DELETE' if dry_run else 'DELETE'), 'AnalysisRun',
              obj['metadata']['name'], flush=True)
    for obj in stale_rs:
        print(('WOULD DELETE' if dry_run else 'DELETE'), 'ReplicaSet',
              obj['metadata']['name'], flush=True)
    if dry_run:
        return

    # Revalidate the entire plan before each write. If an update happens
    # concurrently, stop instead of deleting a newly active revision.
    for kind, planned in (('analysisruns.argoproj.io', stale_ar),
                          ('replicasets.apps', stale_rs)):
        for obj in planned:
            latest, rss, ars, svcs = snapshot()
            if latest['metadata']['uid'] != uid:
                raise RuntimeError(f'{name}: Rollout UID changed during cleanup')
            allowed_rs, allowed_ar, latest_hashes = select_stale(latest, rss, ars, svcs)
            if latest_hashes != hashes:
                raise RuntimeError(f'{name}: active/stable revision changed during cleanup')
            allowed = allowed_ar if kind == 'analysisruns.argoproj.io' else allowed_rs
            if obj['metadata']['uid'] not in {x['metadata']['uid'] for x in allowed}:
                raise RuntimeError(f'{name}: obsolete resource no longer safe: {obj["metadata"]["name"]}')
            oc('delete', kind, obj['metadata']['name'], '-n', NS,
               '--cascade=foreground', '--wait=true', '--timeout=25s')
    print('PASS', name, 'obsolete history pruned', flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--instance', choices=('a', 'b'), required=True)
    p.add_argument('--tier', choices=TIERS, action='append', help='Default: all four tiers')
    p.add_argument('--dry-run', action='store_true', help='Show targets without deleting')
    args = p.parse_args()
    try:
        for tier in (args.tier or TIERS):
            cleanup_one(tier, args.instance, args.dry_run)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError, KeyError) as exc:
        print('ERROR:', exc, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
