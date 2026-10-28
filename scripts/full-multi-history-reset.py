#!/usr/bin/env python3
"""Safely delete prior completed Rollout-owned history for three tiers of one app."""
import argparse,json,subprocess,time,sys
NAMESPACE='full-multi'
FINAL={'Successful','Failed','Inconclusive','Error'}
from full_multi_oc import run_oc
def get(kind,name=None):return json.loads(run_oc('get',kind,*([name] if name else []),'-n',NAMESPACE,'-o','json'))
def owned(o,n,uid):return any(x.get('kind')=='Rollout' and x.get('name')==n and x.get('uid')==uid for x in o.get('metadata',{}).get('ownerReferences',[]))
def cleanup_one(tier,app,deadline,dry_run):
    name=f'{tier}-{app}';stable=None;uid=None
    while True:
        ob=get('rollouts.argoproj.io',name);m=ob['metadata'];s=ob.get('status',{});sp=ob['spec'];h=s.get('stableRS')
        if uid is None:uid=m['uid']
        if uid!=m['uid']:raise RuntimeError(f'{name}: rollout UID changed')
        if stable is not None and stable!=h:raise RuntimeError(f'{name}: stable hash changed')
        marker=sp.get('template',{}).get('metadata',{}).get('annotations',{}).get('demo-rollout-revision')
        env=sp.get('template',{}).get('spec',{}).get('containers',[{}])[0].get('env',[])
        prefix=next((e.get('value') for e in env if e.get('name')=='MESSAGE_PREFIX'),None)
        if marker!='baseline-blue' or (tier!='mariadb' and prefix!=f'Application {app.upper()} - BLUE'):
            raise RuntimeError(f'{name}: not BLUE')
        if s.get('phase')=='Degraded':raise RuntimeError(f'{name}: degraded')
        good=s.get('phase')=='Healthy' and sp.get('replicas')==1 and s.get('updatedReplicas')==1 and s.get('availableReplicas')==1 and h and h==s.get('currentPodHash')
        if good:stable=h;break
        if time.monotonic()>=deadline:raise RuntimeError(f'{name}: readiness timed out: {s}')
        time.sleep(5)
    def invariant():
        o=get('rollouts.argoproj.io',name)
        if o['metadata']['uid']!=uid or o.get('status',{}).get('stableRS')!=stable or o.get('status',{}).get('phase')!='Healthy':raise RuntimeError(f'{name}: stable rollout changed')
    while True:
        invariant();rss=[x for x in get('replicasets.apps')['items'] if owned(x,name,uid)]
        ars=[x for x in get('analysisruns.argoproj.io')['items'] if owned(x,name,uid)]
        main=f'{name}-{stable}'
        if len([x for x in rss if x['metadata']['name']==main])!=1:raise RuntimeError(f'{name}: stable RS missing')
        old=[x for x in rss if x['metadata']['name']!=main]
        live=[x['metadata']['name'] for x in old if x.get('status',{}).get('replicas',0)!=0 or x['spec'].get('replicas',0)!=0]
        active=[x['metadata']['name'] for x in ars if x.get('status',{}).get('phase') not in FINAL]
        if not live and not active:break
        if time.monotonic()>=deadline:raise RuntimeError(f'{name}: live old RS={live}, active runs={active}')
        time.sleep(5)
    print(f'{name}: protect {main}; remove {len(old)} old RS, {len(ars)} AnalysisRuns')
    if dry_run:return
    for kind,items in (('analysisruns.argoproj.io',ars),('replicasets.apps',old)):
        for ob in items:
            invariant();o=get(kind,ob['metadata']['name'])
            if not owned(o,name,uid):raise RuntimeError('owner changed')
            if kind=='replicasets.apps' and (o.get('spec',{}).get('replicas',0) or o.get('status',{}).get('replicas',0)):raise RuntimeError('live RS')
            if kind=='analysisruns.argoproj.io' and o.get('status',{}).get('phase') not in FINAL:raise RuntimeError('active run')
            print('DELETE',kind,o['metadata']['name'],flush=True)
            run_oc('delete',kind,o['metadata']['name'],'-n',NAMESPACE,'--wait=true','--timeout=30s')
    invariant()
    if any(owned(x,name,uid) and x['metadata']['name']!=main for x in get('replicasets.apps')['items']):raise RuntimeError('RS cleanup incomplete')
    if any(owned(x,name,uid) for x in get('analysisruns.argoproj.io')['items']):raise RuntimeError('AnalysisRun cleanup incomplete')
    print('PASS',name,'history cleared')
if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--instance',choices=['a','b'],required=True);p.add_argument('--dry-run',action='store_true');p.add_argument('--timeout-seconds',type=int,default=600);a=p.parse_args()
    try:
        deadline=time.monotonic()+a.timeout_seconds
        for tier in ('mariadb','backend','front'):cleanup_one(tier,a.instance,deadline,a.dry_run)
    except Exception as exc:
        print('ERROR:',exc,file=sys.stderr);sys.exit(1)
