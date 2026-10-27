#!/usr/bin/env python3
"""Offline contract verification and BLUE/YELLOW manifest changes for full-multi."""
import argparse
from pathlib import Path
import re
ROOT=Path(__file__).resolve().parent.parent
D=ROOT/'full-multi'
TIERS=('front','backend','mariadb')

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
                assert 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-tracing-v2' in s
            else:
                assert '      autoPromotionEnabled: false' in s, path
                assert '          emptyDir: {}' in s, path
                assert 'activeService:' in s and 'previewService:' in s, path
            names.append(f'{tier}-{app}')
    assert len(names)==len(set(names))==6
    a=(D/'front-virtualservice.yaml').read_text()
    for name in ('primary','secondary','front-b-force-canary','front-a-force-canary','x-app-target','x-front-track'):
        assert name in a, name
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
    print('PASS: six Rollouts, routing and observability contracts')

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
        p.write_text(s)
    verify();print(f'Set {instance.upper()} to {col} marker={marker}')
if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('command',choices=['verify','set']);ap.add_argument('--instance',choices=['a','b']);ap.add_argument('--color',choices=['blue','yellow']);x=ap.parse_args()
    if x.command=='set':
        if not x.instance or not x.color:ap.error('set requires --instance and --color')
        setcolor(x.color,x.instance)
    else:verify()
