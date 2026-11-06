#!/usr/bin/env python3
"""Offline topology contract and deterministic full-multi lifecycle template changes."""
import argparse
from pathlib import Path
import re
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parent.parent
D=ROOT/'full-multi'
TIERS=('front','backend','crud')
DB_IMAGES={'blue':'english-literature-20-v1','green':'classicos-brasileiros-20-v1'}

def verify():
    expected=sorted(p.name for p in D.glob('*.yaml') if p.name!='kustomization.yaml')
    manifest=(D/'kustomization.yaml').read_text()
    found=sorted(re.findall(r'^  - ([\w-]+\.yaml)$',manifest,re.M))
    assert found==expected,("kustomization resources",sorted(set(expected)-set(found)),sorted(set(found)-set(expected)))
    rollouts=set(re.findall(r'(?m)^  name: ([\w-]+)$', '\n'.join((D/f'rollout-{tier}-{app}.yaml').read_text().split('spec:')[0] for tier in TIERS for app in 'ab')))
    assert rollouts=={f'{tier}-{app}' for tier in TIERS for app in 'ab'},rollouts
    assert len(list(D.glob('rollout-*.yaml')))==6
    assert not list(D.glob('rollout-mariadb-*.yaml'))
    assert not list(D.glob('service-mariadb-*-stable.yaml'))
    assert not list(D.glob('service-mariadb-*-canary.yaml'))
    for app in 'ab':
        for tier in ('front','backend'):
            p=D/f'rollout-{tier}-{app}.yaml';s=p.read_text()
            assert '  replicas: 1\n' in s,p
            assert '      trafficRouting:' in s and '      steps:' in s,p
            steps=s.split('      steps:\n',1)[1]
            assert re.findall(r'^        - setWeight: (\d+)$',steps,re.M)==['10','25','50'],p
            assert steps.count('        - analysis:')==3,p
            assert steps.rstrip().endswith('- pause: {}'),p
            assert 'demo-rollout-revision:' in s,p
            assert f'Application {app.upper()} - ' in s,p
        cr=(D/f'rollout-crud-{app}.yaml').read_text()
        assert '    blueGreen:' in cr
        assert f'      activeService: crud-{app}-active' in cr
        assert f'      previewService: crud-{app}-preview' in cr
        assert '      autoPromotionEnabled: false' in cr
        assert '            - name: DB_HOST\n' in cr
        assert '            - name: DB_TRACK\n' in cr
        assert 'DB_HOST_CANARY' not in cr and 'DB_HOST_STABLE' not in cr
        match=re.search(r'demo\.crud-track: (blue|green)',cr)
        assert match,app
        track=match.group(1)
        assert f'              value: mariadb-{app}-{track}.full-multi.svc.cluster.local' in cr
        assert f'              value: {track}\n' in cr
        assert 'full-multi-crud-v5' in cr
        for tr,image in DB_IMAGES.items():
            ss=(D/f'statefulset-mariadb-{app}-{tr}.yaml').read_text()
            svc=(D/f'service-mariadb-{app}-{tr}.yaml').read_text()
            assert f'kind: StatefulSet' in ss and f'mariadb:{image}' in ss
            assert 'volumeClaimTemplates:' in ss and 'ReadWriteOnce' in ss
            assert f'    app: mariadb-{app}-{tr}' in svc
            assert f'db-credentials-{app}' in ss
            fixed=(D/f'service-crud-{app}-{tr}.yaml').read_text()
            assert f'demo.crud-track: {tr}' in fixed
        for role in ('active','preview'):
            svc=(D/f'service-crud-{app}-{role}.yaml').read_text()
            assert f'  name: crud-{app}-{role}' in svc
            assert 'demo.crud-track' not in svc
        vs=(D/f'crud-{app}-virtualservice.yaml').read_text()
        for needle in (f'crud-{app}-blue',f'crud-{app}-green',f'crud-{app}-active',
                       'x-db-track:', 'exact: green','exact: stable'):
            assert needle in vs,(app,needle)
        assert 'x-crud-db-target' not in vs
        backend=(D/f'rollout-backend-{app}.yaml').read_text()
        assert f'http://crud-{app}-blue.full-multi.svc.cluster.local:8080/api/books' in backend
        assert '            - name: DB_PASSWORD' not in backend
        front=(D/f'rollout-front-{app}.yaml').read_text()
        assert f'http://backend-{app}-stable.full-multi.svc.cluster.local:5000/api/books' in front
    go=(D/'bookshelf/main.go').read_text()
    assert 'os.Getenv("DB_HOST")' in go and 'os.Getenv("DB_TRACK")' in go
    assert 's.queryBooks(r.Context())' in go and 'x-crud-db-target' not in go
    assert 'db.ExecContext(' not in go, 'CRUD must not execute database writes'
    assert 'INSERT IGNORE INTO books' not in go, 'CRUD must not insert books'
    assert 'CREATE TABLE IF NOT EXISTS books' not in go, 'CRUD must not modify schema'
    assert 'traceparent' in go and 'propagateTraceHeaders' in go
    assert 'otel-tracing' in (D/'telemetry.yaml').read_text()
    assert (D/'podmonitor-istio-proxies.yaml').exists()
    print('PASS: six Rollouts (4 Canary + 2 CRUD BlueGreen), four persistent language databases, fixed DB binding and Istio routing')

def setcolor(color,instance):
    verify()
    assert color in ('blue','yellow') and instance in ('a','b')
    col=color.upper()
    marker='baseline-blue' if color=='blue' else 'candidate-yellow-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    changes={}
    for tier in TIERS:
        p=D/f'rollout-{tier}-{instance}.yaml'
        s=p.read_text()
        s,n=re.subn(r'(        demo-rollout-revision: )\S+',lambda m:m[1]+marker,s)
        assert n==1,p
        if tier in ('front','backend'):
            s,n=re.subn(r'(              value: "Application [AB] - )(BLUE|YELLOW)(")',lambda m:m[1]+col+m[3],s)
            assert n==1,p
            s,n=re.subn(r'(                value: )(BLUE|YELLOW)$',lambda m:m[1]+col,s,flags=re.M)
            assert n==3,p
        else:
            tr='blue' if color=='blue' else 'green'
            s,n=re.subn(r'(        demo\.crud-track: )(blue|green)',lambda m:m[1]+tr,s)
            assert n==1,p
            s,n=re.subn(r'(              value: mariadb-'+instance+r'-)(blue|green)(\.full-multi\.svc\.cluster\.local)',
                        lambda m:m[1]+tr+m[3],s)
            assert n==1,p
            s,n=re.subn(r'(?m)(            - name: DB_TRACK\n              value: )(blue|green)$',lambda m:m[1]+tr,s)
            assert n==1,p
        changes[p]=s
    for p,s in changes.items():p.write_text(s)
    verify()
    print(f'Set application {instance.upper()} Front/Backend to {col}, CRUD to {"blue/English" if color=="blue" else "green/Portuguese"} ({marker})')

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['verify','set'])
    p.add_argument('--instance',choices=('a','b'))
    p.add_argument('--color',choices=('blue','yellow'))
    args=p.parse_args()
    if args.command=='set':
        if not args.instance or not args.color:p.error('set requires --instance and --color')
        setcolor(args.color,args.instance)
    else:verify()
