#!/usr/bin/env python3
"""Offline contract verification and BLUE/YELLOW manifest changes for full-multi."""
import argparse
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parent.parent
D=ROOT/'full-multi'
TIERS=('front','backend','mariadb')
SERVICE_CLEANUP_PHASE = 'finalize'
FRONT_DELEGATION_PHASE = 'stage'
MARIA_BLUE_IMAGE='ghcr.io/gstephan76/argo-sm-mariadb:english-literature-20-v1'
MARIA_GREEN_IMAGE='ghcr.io/gstephan76/argo-sm-mariadb:classicos-brasileiros-20-v1'

def verify():
    expected=sorted(p.name for p in D.glob('*.yaml') if p.name != 'kustomization.yaml')
    manifest=(D/'kustomization.yaml').read_text()
    found=sorted(re.findall(r'^  - ([\w-]+\.yaml)$',manifest,re.M))
    if found!=expected:raise SystemExit(f'Kustomization mismatch: expected {expected}, found {found}')
    names=[]
    for app in 'ab':
        for tier in TIERS:
            path=D/f'rollout-{tier}-{app}.yaml'
            s=path.read_text()
            assert f'  name: {tier}-{app}\n' in s, path
            assert '  replicas: 1\n' in s, path
            assert '  analysis:\n    successfulRunHistoryLimit: 2\n    unsuccessfulRunHistoryLimit: 2\n' in s, path
            assert '    matchLabels:' in s
            assert '        demo-rollout-revision:' in s
            assert (f'{tier}-{app}-stable' in s and f'{tier}-{app}-canary' in s)
            if tier!='mariadb':
                assert '      trafficRouting:' in s and '      steps:' in s
                # With Istio routing, four HTTP Rollouts ramp to 50% and
                # hold indefinitely until manual promotion. The pause is
                # step index 6 (0-based): 10/analyze,25/analyze,50/analyze,pause.
                steps=s.split('      steps:\n',1)[1]
                assert re.findall(r'^        - setWeight: (\d+)$',steps,re.M)==['10','25','50'],path
                assert steps.count('        - analysis:')==3,path
                assert steps.rstrip().endswith('- pause: {}'),path
                assert steps.count('        - pause: {}')==1,path
                assert s.count('value: BLUE')==3 or s.count('value: YELLOW')==3,path
                assert 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-release-theme-v2' in s
            else:
                assert '      autoPromotionEnabled: false' in s, path
                assert '          emptyDir: {}' in s, path
                assert 'activeService:' in s and 'previewService:' in s, path
                db_marker=re.search(r'^        demo-rollout-revision: (\S+)$',s,re.M)
                if not db_marker:
                    raise SystemExit(f'{path}: missing MariaDB revision marker')
                if db_marker.group(1)=='baseline-blue':
                    expected_db_image=MARIA_BLUE_IMAGE
                elif db_marker.group(1).startswith('candidate-yellow-'):
                    expected_db_image=MARIA_GREEN_IMAGE
                else:
                    raise SystemExit(f'{path}: unknown MariaDB revision marker: {db_marker.group(1)}')
                assert f'          image: {expected_db_image}\n' in s, (path,expected_db_image)
            names.append(f'{tier}-{app}')
    assert len(names)==len(set(names))==6  # three demo lifecycle Rollouts per A/B
    for app in 'ab':
        r=(D/f'rollout-crud-{app}.yaml').read_text()
        assert f'  name: crud-{app}\n' in r
        assert '  replicas: 1\n' in r
        assert '  analysis:\n    successfulRunHistoryLimit: 2\n    unsuccessfulRunHistoryLimit: 2\n' in r
        assert '    canary:\n' in r and '      maxSurge: 1' in r
        assert '      maxUnavailable: 0' in r
        assert '            - name: ROLE\n              value: crud' in r
        assert '            - name: DB_PASSWORD' in r
        assert 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-crud-v3' in r
        b=(D/f'rollout-backend-{app}.yaml').read_text()
        assert f'http://crud-{app}-blue.full-multi.svc.cluster.local:8080/api/books' in b
        assert '            - name: DB_PASSWORD' not in b
        vs=(D/f'crud-{app}-virtualservice.yaml').read_text()
        for needle in ('x-db-track:', 'exact: green', 'exact: canary',
                       'x-crud-db-target: canary', 'x-crud-db-target: stable',
                       f'crud-{app}-green.full-multi.svc.cluster.local',
                       f'crud-{app}-blue.full-multi.svc.cluster.local'):
            assert needle in vs, (app, needle)
        for suffix in ('-blue', '-green'):
            s=(D/f'service-crud-{app}{suffix}.yaml').read_text()
            assert f'  name: crud-{app}{suffix}\n' in s
            assert f'    app: crud-{app}\n' in s
    # Frontend ingress delegation: root selects A/B, delegates own traffic.
    root=(D/'front-virtualservice.yaml').read_text()
    for app, route in (('a','primary'),('b','secondary')):
        delegate=(D/f'front-{app}-router.yaml').read_text()
        for key in (f'front-{app}-force-stable', f'front-{app}-force-canary',
                    f'front-{app}-stable', f'front-{app}-canary', route, 'x-front-track'):
            assert key in delegate, (app,key)
        assert '  hosts:' not in delegate and '  gateways:' not in delegate
        weights=re.search(r'(?m)^    - name: '+route+r'\n(.*?)(?=^    - name: |\Z)',delegate,re.S|re.M)
        assert weights, (app,'missing weighted route')
        pairs=[int(w) for w in re.findall(r'(?m)^          weight: (\d+)$',weights.group(1))]
        assert len(pairs)==2 and sum(pairs)==100, (app,'weights',pairs)
        front=(D/f'rollout-front-{app}.yaml').read_text()
        expected_vs='full-multi-front-router' if FRONT_DELEGATION_PHASE=='stage' else f'front-{app}-router'
        assert re.search(r'(?m)^            name: '+expected_vs+r'$',front), (app,expected_vs)
    if FRONT_DELEGATION_PHASE=='stage':
        assert 'name: front-a-force-canary' in root and 'name: front-b-force-canary' in root
        assert 'delegate:' not in root
    else:
        assert 'name: application-b' in root and 'name: application-a-default' in root
        assert 'x-app-target:' in root
        assert 'name: front-a-router' in root and 'name: front-b-router' in root
        assert 'name: primary' not in root and 'name: secondary' not in root
    for app in 'ab':
        a=(D/f'backend-{app}-virtualservice.yaml').read_text()
        for name in (f'backend-{app}-default','x-backend-track',f'backend-{app}-canary'):
            assert name in a,name
        for f in ('front','backend'):
            s=(D/f'rollout-{f}-{app}.yaml').read_text()
            assert f'Application {app.upper()} - ' in s
    script=(D/'bookshelf/main.go').read_text()
    for name in ('x-backend-track','x-db-track','DB_HOST_CANARY','DB_HOST_STABLE','traceparent','propagateTraceHeaders'):
        assert name in script,name
    assert 'otel-tracing' in (D/'telemetry.yaml').read_text()
    assert (D/'podmonitor-istio-proxies.yaml').is_file()
    # Verify a deliberate, bounded Service topology and updated in-mesh URLs.
    for app in 'ab':
        for tier, suffixes in (
            ('front', ('-stable', '-canary')),
            ('backend', ('', '-stable', '-canary') if SERVICE_CLEANUP_PHASE == 'migrate' else ('-stable', '-canary')),
            ('crud', ('', '-blue', '-green') if SERVICE_CLEANUP_PHASE == 'migrate' else ('-blue', '-green')),
            ('mariadb', ('-stable', '-canary')),
        ):
            actual = {p.name for p in D.glob(f'service-{tier}-{app}*.yaml')}
            expected = {f'service-{tier}-{app}{suffix}.yaml' for suffix in suffixes}
            assert actual == expected, (tier, app, actual, expected)
        front = (D / f'rollout-front-{app}.yaml').read_text()
        backend = (D / f'rollout-backend-{app}.yaml').read_text()
        assert f'http://backend-{app}-stable.full-multi.svc.cluster.local:5000/api/books' in front
        assert f'http://crud-{app}-blue.full-multi.svc.cluster.local:8080/api/books' in backend
        for tier, final_suffix in (('backend', 'stable'), ('crud', 'blue')):
            vs = (D / f'{tier}-{app}-virtualservice.yaml').read_text()
            new_host = f'{tier}-{app}-{final_suffix}.full-multi.svc.cluster.local'
            old_host = f'{tier}-{app}.full-multi.svc.cluster.local'
            hosts_part = vs.split('  hosts:\n', 1)[1].split('  gateways:', 1)[0]
            assert f'    - {new_host}\n' in hosts_part, (tier, app, new_host)
            assert (f'    - {old_host}\n' in hosts_part) == (SERVICE_CLEANUP_PHASE == 'migrate'), (tier, app, old_host)
    print('PASS: eight Rollouts, CRUD DB routing and observability contracts')

def setcolor(color,instance):
    col=color.upper()
    from datetime import datetime,timezone
    marker='baseline-blue' if color=='blue' else 'candidate-yellow-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    for tier in TIERS:
        p=D/f'rollout-{tier}-{instance}.yaml';s=p.read_text()
        s,n=re.subn(r'(        demo-rollout-revision: )\S+',lambda m:m[1]+marker,s)
        if n!=1:raise SystemExit(f'missing single revision marker in {p}')
        if tier!='mariadb':
            s,n=re.subn(r'(              value: "Application [AB] - )(BLUE|YELLOW)(")',lambda m:m[1]+col+m[3],s)
            if n!=1:raise SystemExit(f'missing single MESSAGE_PREFIX in {p}')
            s,n=re.subn(r'(                value: )(BLUE|YELLOW)$',lambda m:m[1]+col,s,flags=re.M)
            if n!=3:raise SystemExit(f'missing three analysis colors in {p}')
        else:
            db_image=MARIA_BLUE_IMAGE if color=='blue' else MARIA_GREEN_IMAGE
            s,n=re.subn(r'(^          image: )ghcr\.io/gstephan76/argo-sm-mariadb:[^\s]+$',
                        lambda m: m[1]+db_image,s,flags=re.M)
            if n!=1:raise SystemExit(f'missing MariaDB image reference in {p}')
        p.write_text(s)
    verify();print(f'Set {instance.upper()} to {col} marker={marker}')
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['verify','set']);ap.add_argument('--instance',choices=['a','b']);ap.add_argument('--color',choices=['blue','yellow']);x=ap.parse_args()
    if x.command=='set':
        if not x.instance or not x.color:ap.error('set requires --instance and --color')
        setcolor(x.color,x.instance)
    else:verify()
