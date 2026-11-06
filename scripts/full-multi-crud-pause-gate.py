#!/usr/bin/env python3
"""Read-only safety gate: a paused CRUD rollout MUST serve both language DBs.

Verifies A or B independently. Uses actual Pod and EndpointSlice membership;
checks active/preview hashes, database host binding, PVC independence,
and readiness, not just a Rollout phase string.
"""
import argparse
import json
import subprocess
import sys

NS='full-multi'

class Failed(RuntimeError):
    pass

def need(ok, why):
    if not ok: raise Failed(why)

def oc(*args):
    cmd=['oc','-n',NS,'get',*args,'-o','json']
    cp=subprocess.run(cmd, text=True, capture_output=True, timeout=35)
    need(cp.returncode==0, ' '.join(cmd)+': '+cp.stderr.strip())
    return json.loads(cp.stdout)

def env_of(pod, name):
    ct=next((c for c in pod['spec']['containers'] if c['name']=='crud'),None)
    need(ct is not None, pod['metadata']['name']+': missing crud container')
    return next((e.get('value') for e in ct.get('env',[]) if e['name']==name),None)

def ready(pod):
    conditions=pod.get('status',{}).get('conditions') or []
    return (not pod['metadata'].get('deletionTimestamp') and
            pod.get('status',{}).get('phase')=='Running' and
            any(c['type']=='Ready' and c['status']=='True' for c in conditions))

def active_endpoint_pods(name):
    slices=oc('endpointslices.discovery.k8s.io','-l',f'kubernetes.io/service-name={name}')['items']
    return {(e.get('targetRef') or {}).get('name') for s in slices for e in s.get('endpoints',[])
            if e.get('conditions',{}).get('ready',True) and (e.get('targetRef') or {}).get('kind')=='Pod'}

def verify(app):
    name=f'crud-{app}'
    roll=oc('rollout',name)
    bg=roll.get('spec',{}).get('strategy',{}).get('blueGreen',{})
    need(bg.get('autoPromotionEnabled') is False and 'autoPromotionSeconds' not in bg,
         name+': automatic promotion is not disabled')
    need(bg.get('activeService')==name+'-active' and bg.get('previewService')==name+'-preview',
         name+': wrong managed Services')
    need(bg.get('previewReplicaCount')==1, name+': preview must have one replica')
    status=roll.get('status') or {}
    need(status.get('phase')=='Paused', name+': must be Paused, got '+str(status.get('phase')))
    stable=status.get('stableRS')
    current=status.get('currentPodHash')
    need(stable and current and stable!=current, name+': different active/preview hashes required')
    allpods=oc('pods','-l',f'app={name}')['items']
    selected={}
    pvnames=[]
    for track,h in [('blue',stable),('green',current)]:
        expected_db=f'mariadb-{app}-{track}'
        expected_host=expected_db+'.full-multi.svc.cluster.local'
        matches=[p for p in allpods if ready(p) and
                 p['metadata'].get('labels',{}).get('demo.crud-track')==track and
                 p['metadata'].get('labels',{}).get('rollouts-pod-template-hash')==h and
                 env_of(p,'DB_HOST')==expected_host and env_of(p,'DB_TRACK')==track and
                 env_of(p,'APP_ID')==app.upper()]
        need(matches, f'{name} {track}: no Ready pod with matching ReplicaSet and independent database binding')
        selected[track]={p['metadata']['name'] for p in matches}
        sts=oc('statefulset',expected_db)
        need(sts.get('status',{}).get('readyReplicas',0)>=1,
             expected_db+': StatefulSet not Ready')
        pvcname='mariadb-data-'+expected_db+'-0'
        pvc=oc('pvc',pvcname)
        need(pvc.get('status',{}).get('phase')=='Bound',pvcname+': not Bound')
        pv= pvc.get('spec',{}).get('volumeName')
        need(bool(pv),pvcname+': no dedicated PV')
        pvnames.append(pv)
    need(len(set(pvnames))==2,name+': Blue and Green share a PV')
    for svc,track in [('blue','blue'),('green','green'),('active','blue'),('preview','green')]:
        found=active_endpoint_pods(name+'-'+svc)
        need(bool(found & selected[track]),
             f'{name}-{svc}: no Ready endpoints targeting the expected {track} revision')
        need(not (found & selected['green' if track=='blue' else 'blue']),
             f'{name}-{svc}: endpoints cross the Blue/Green boundary')
    print(f'PASS {name}: Paused; Blue active + Green preview Ready, isolated DBs/PVCs, 4 routed Services')
    return pvnames

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--instance',choices=('a','b','all'),default='all')
    a=p.parse_args()
    try:
        pv=[]
        for app in ('ab' if a.instance=='all' else a.instance):
            pv.extend(verify(app))
        need(len(set(pv))==len(pv), 'Applications A/B share a persistent volume')
        print('PASS independent PVC volumes for every verified database')
    except (Failed,ValueError,KeyError,subprocess.TimeoutExpired) as exc:
        print('FAIL CRUD PAUSE GATE:',exc,file=sys.stderr)
        sys.exit(1)
