#!/usr/bin/env python3
"""Wait for GitOps revision observation during protected prune-disabled migration.

Does not infer Synced while obsolete resources are intentionally retained;
subsequent rollout/endpoint validation checks real deployment effects.
"""
import argparse
import json
import subprocess
import sys
import time

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--revision',required=True)
    p.add_argument('--timeout',type=int,default=900)
    p.add_argument('--poll',type=int,default=5)
    a=p.parse_args()
    deadline=time.monotonic()+a.timeout
    observed=''
    while time.monotonic()<deadline:
        cp=subprocess.run(['oc','-n','openshift-gitops','get','application','full-multi','-o','json'],
                          capture_output=True,text=True,timeout=30)
        if cp.returncode:
            print('ERROR querying Argo CD:',cp.stderr,file=sys.stderr)
            sys.exit(1)
        app=json.loads(cp.stdout)
        status=app.get('status') or {}
        rev=(status.get('sync') or {}).get('revision')
        phase=(status.get('operationState') or {}).get('phase')
        if rev==a.revision:
            print('PASS: Argo CD observed requested revision',rev,'sync=',(status.get('sync') or {}).get('status'))
            sys.exit(0)
        if phase in ('Failed','Error'):
            print('ERROR: Argo CD failed:',(status.get('operationState') or {}).get('message'),file=sys.stderr)
            sys.exit(1)
        if rev!=observed:
            print('WAIT GitOps observed',rev,'need',a.revision)
            observed=rev
        time.sleep(a.poll)
    print('ERROR: Argo CD has not observed expected revision',a.revision,file=sys.stderr)
    sys.exit(1)
