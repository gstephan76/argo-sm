#!/usr/bin/env python3
"""Offline topology contract and deterministic full-multi lifecycle template changes."""
import argparse
from pathlib import Path
import re
import subprocess
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
        assert '      previewReplicaCount: 1' in cr
        assert 'autoPromotionSeconds:' not in cr
        assert '      scaleDownDelaySeconds: 300' in cr
        assert '            - name: DB_HOST\n' in cr
        assert '            - name: DB_TRACK\n' in cr
        assert 'DB_HOST_CANARY' not in cr and 'DB_HOST_STABLE' not in cr
        match=re.search(r'demo\.crud-track: (blue|green)',cr)
        assert match,app
        track=match.group(1)
        assert f'              value: mariadb-{app}-{track}.multi-ambient.svc.cluster.local' in cr
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
        vs=(D/f'crud-{app}-httproute.yaml').read_text()
        for needle in (f'crud-{app}-blue',f'crud-{app}-green',f'crud-{app}-active',
                       'x-db-track', 'value: green','value: stable'):
            assert needle in vs,(app,needle)
        assert 'x-crud-db-target' not in vs
        backend=(D/f'rollout-backend-{app}.yaml').read_text()
        assert f'http://crud-{app}-blue.multi-ambient.svc.cluster.local:8080/api/books' in backend
        assert '            - name: DB_PASSWORD' not in backend
        front=(D/f'rollout-front-{app}.yaml').read_text()
        assert f'http://backend-{app}-stable.multi-ambient.svc.cluster.local:5000/api/books' in front
    go=(D/'bookshelf/main.go').read_text()
    assert 'os.Getenv("DB_HOST")' in go and 'os.Getenv("DB_TRACK")' in go
    assert 's.queryBooks(r.Context())' in go and 'x-crud-db-target' not in go
    assert 'db.ExecContext(' not in go, 'CRUD must not execute database writes'
    assert 'INSERT IGNORE INTO books' not in go, 'CRUD must not insert books'
    assert 'CREATE TABLE IF NOT EXISTS books' not in go, 'CRUD must not modify schema'
    assert 'traceparent' in go and 'propagateTraceHeaders' in go
    assert 'otel-tracing' in (D/'telemetry.yaml').read_text()
    assert (D/'podmonitor-istio-proxies.yaml').exists()
    print('PASS: six Rollouts, four independent MariaDB PVCs, explicit CRUD promotion, indefinite Blue/Green preview')

def transform(s, tier, instance, color, marker):
    col=color.upper()
    s,n=re.subn(r'(        demo-rollout-revision: )\S+',lambda m:m[1]+marker,s)
    assert n==1, tier
    if tier in ('front','backend'):
        s,n=re.subn(r'(              value: "Application [AB] - )(BLUE|YELLOW)(")',lambda m:m[1]+col+m[3],s)
        assert n==1, tier
        s,n=re.subn(r'(                value: )(BLUE|YELLOW)$',lambda m:m[1]+col,s,flags=re.M)
        assert n==3, tier
    else:
        tr='blue' if color=='blue' else 'green'
        s,n=re.subn(r'(        demo\.crud-track: )(blue|green)',lambda m:m[1]+tr,s)
        assert n==1, tier
        s,n=re.subn(r'(              value: mariadb-'+instance+r'-)(blue|green)(\.multi-ambient\.svc\.cluster\.local)',
                    lambda m:m[1]+tr+m[3],s)
        assert n==1, tier
        s,n=re.subn(r'(?m)(            - name: DB_TRACK\n              value: )(blue|green)$',lambda m:m[1]+tr,s)
        assert n==1, tier
    return s

def setcolor(color,instance):
    verify()
    assert color in ('blue','yellow') and instance in ('a','b')
    if color == 'yellow':
        texts = [(D/f'rollout-{tier}-{instance}.yaml').read_text() for tier in TIERS]
        markers = [re.search(r'(?m)^        demo-rollout-revision: (\S+)$', s).group(1) for s in texts]
        tracks = re.findall(r'(?m)^        demo\.crud-track: (blue|green)$', texts[2])
        if len(set(markers)) == 1 and re.fullmatch(r'candidate-yellow-\d{8}T\d{6}Z', markers[0]):
            assert tracks == ['green'], 'Existing candidate has an inconsistent CRUD track'
            for s in texts[:2]:
                assert f'Application {instance.upper()} - YELLOW' in s
                assert re.findall(r'(?m)^                value: (BLUE|YELLOW)$', s) == ['YELLOW'] * 3
            print(f'Application {instance.upper()} candidate already exists ({markers[0]}); unchanged')
            return
        assert tracks == ['blue'] and markers == ['baseline-blue'] * 3, \
            'Refusing to replace an inconsistent or unrecognized candidate; inspect the templates'
    marker='baseline-blue' if color=='blue' else 'candidate-yellow-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    changes={}
    for tier in TIERS:
        p=D/f'rollout-{tier}-{instance}.yaml'
        changes[p]=transform(p.read_text(), tier, instance, color, marker)
    for p,s in changes.items():p.write_text(s)
    verify()
    print(f'Set application {instance.upper()} Front/Backend to {color.upper()}, CRUD to {"blue/English" if color=="blue" else "green/Portuguese"} ({marker})')

def verify_pending(instance, color, base, head):
    """Verify a single pending workflow commit against its exact transformation."""
    def git(*args):
        return subprocess.run(['git', *args], cwd=ROOT, text=True, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout
    assert re.fullmatch(r'[0-9a-f]{40,64}', base) and re.fullmatch(r'[0-9a-f]{40,64}', head)
    assert git('rev-parse', '--verify', 'refs/full-multi/pending').strip()==head, 'Pending workflow reference does not identify HEAD'
    assert git('rev-list', '--parents', '-n', '1', head).strip().split() == [head,base], 'Pending commit must have origin/multi-ambient as its sole parent'
    apps='ab' if instance=='all' else instance
    subject=('Prepare full-multi: deterministic Blue baseline' if instance=='all' else
             f'Set full-multi instance {instance} {color}')
    assert git('show', '-s', '--format=%s', head).strip()==subject, 'Unexpected pending commit subject'
    allowed={f'full-multi/rollout-{tier}-{app}.yaml' for app in apps for tier in TIERS}
    changed=git('diff', '--name-status', '--no-renames', base, head).splitlines()
    assert changed and all(line.startswith('M\t') and line[2:] in allowed for line in changed), 'Pending commit changes unexpected paths'
    assert not git('diff', '--summary', base, head).strip(), 'Pending commit changes file modes or types'
    verify()
    for app in apps:
        paths={tier:f'full-multi/rollout-{tier}-{app}.yaml' for tier in TIERS}
        after={tier:git('show', f'{head}:{path}') for tier,path in paths.items()}
        markers=[re.search(r'(?m)^        demo-rollout-revision: (\S+)$', after[t]).group(1) for t in TIERS]
        assert len(set(markers))==1, 'Pending rollout markers differ'
        marker=markers[0]
        assert (marker=='baseline-blue' if color=='blue' else
                re.fullmatch(r'candidate-yellow-\d{8}T\d{6}Z', marker)), 'Unexpected pending revision marker'
        for tier,path in paths.items():
            before=git('show', f'{base}:{path}')
            if color=='yellow':
                assert '        demo-rollout-revision: baseline-blue\n' in before, 'Yellow candidate must start from Blue'
            assert transform(before,tier,app,color,marker)==after[tier], f'Unexpected change in {path}'
    print(f'PASS pending workflow commit {head[:12]} exactly matches {instance}/{color} transformation')

if __name__=='__main__':
    p=argparse.ArgumentParser()
    p.add_argument('command',choices=['verify','set','verify-pending'])
    p.add_argument('--instance',choices=('a','b','all'))
    p.add_argument('--color',choices=('blue','yellow'))
    p.add_argument('--base')
    p.add_argument('--head')
    args=p.parse_args()
    if args.command=='set':
        if args.instance not in ('a','b') or not args.color:p.error('set requires --instance a|b and --color')
        setcolor(args.color,args.instance)
    elif args.command=='verify-pending':
        if not args.instance or not args.color or not args.base or not args.head or (args.instance=='all' and args.color!='blue'):
            p.error('verify-pending requires --instance, --color, --base and --head')
        verify_pending(args.instance,args.color,args.base,args.head)
    else:verify()
