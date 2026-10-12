#!/usr/bin/env python3
"""Validate or set V1/V2 HTTPD ConfigMap on one of two Istio Rollouts (offline)."""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
PARTS = ROOT / 'full-single-service'
SPECS = {i: PARTS / f'rollout-{i}.yaml' for i in ('a', 'b')}
MANIFEST = ROOT / 'argocd' / 'application-full-single-service.yaml'
FILES = {
    'namespace.yaml', 'configmap-httpd-v1.yaml', 'configmap-httpd-v2.yaml',
    'ingress-gateway.yaml', 'gateway.yaml', 'route-a.yaml', 'route-b.yaml',
    'virtualservice.yaml', 'service-a-stable.yaml', 'service-a-canary.yaml',
    'service-b-stable.yaml', 'service-b-canary.yaml',
    'analysis-template-a.yaml', 'analysis-template-b.yaml',
    'rollout-a.yaml', 'rollout-b.yaml', 'podmonitor-istio-proxies.yaml',
}
IMAGE = 'registry.access.redhat.com/ubi9/httpd-24:latest'

def abort(message):
    raise SystemExit('ERROR: ' + message)

def replace_n(text, pattern, replacement, expected, desc):
    result, count = re.subn(pattern, lambda _: replacement, text, flags=re.M)
    if count != expected:
        abort(f'{desc}: expected {expected} matches, found {count}')
    return result

def verify(instance):
    raw = (PARTS / 'kustomization.yaml').read_text()
    listed = re.findall(r'^  - ([a-z0-9-]+\.yaml)$', raw, re.M)
    if set(listed) != FILES or len(listed) != len(FILES):
        abort('Kustomization resources differ from expected two-instance manifest set')
    for name in FILES:
        if not (PARTS / name).is_file(): abort('Missing resource ' + name)
    for version in ('v1', 'v2'):
        cm = (PARTS / f'configmap-httpd-{version}.yaml').read_text()
        if f'name: httpd-page-{version}' not in cm or f'Revision {version.upper()}' not in cm:
            abort('Bad ConfigMap ' + version)
    app = MANIFEST.read_text()
    for fragment in ('repoURL: https://github.com/gstephan76/argo-sm.git',
                     'path: full-single-service', 'namespace: full-single-service'):
        if fragment not in app: abort('Argo CD Application mismatch: '+fragment)
    vs = (PARTS / 'virtualservice.yaml').read_text()
    for fragment in ('name: primary', 'name: secondary',
                     'host: full-single-service-a-stable',
                     'host: full-single-service-b-stable'):
        if fragment not in vs: abort('VirtualService mismatch: '+fragment)
    spec = SPECS[instance].read_text()
    if len(re.findall('image: '+re.escape(IMAGE)+'$',spec,re.M)) != 1:
        abort('Expected exact UBI9 HTTPD container image')
    if 'httpGet:' in spec or spec.count('tcpSocket:') != 2:
        abort('Both probes must use tcpSocket, not HTTP')
    if 'mountPath: /var/www/html/index.html' not in spec:
        abort('Missing Knative-style document root mount')
    if f'name: full-single-service-{instance}\n' not in spec:
        abort('Unexpected Rollout resource name')
    versions = re.findall(r'^            name: httpd-page-(v[12])$',spec,re.M)
    markers = re.findall(r'^        demo-rollout-revision: (\S+)$',spec,re.M)
    expected = re.findall(r'^                value: (v[12])$',spec,re.M)
    if len(versions)!=1 or len(markers)!=1 or len(expected)!=5 or any(e!=versions[0] for e in expected):
        abort('ConfigMap, revision marker, and five analysis gates do not match')
    v, marker=versions[0], markers[0]
    if v=='v1' and marker!='baseline-v1': abort('V1 must use baseline-v1')
    if v=='v2' and not re.fullmatch(r'candidate-v2-[0-9]{8}T[0-9]{6}Z',marker):
        abort('V2 marker must have timestamp candidate-v2-YYYYMMDDTHHMMSSZ')
    analysis=(PARTS / f'analysis-template-{instance}.yaml').read_text()
    if 'expected-version' not in analysis or 'Revision V1' not in analysis or 'Revision V2' not in analysis:
        abort('Analysis must check V1 or V2 candidate page')
    return v,marker

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('command',choices=['verify','set'])
    parser.add_argument('--instance',choices=['a','b'],default='a')
    parser.add_argument('--version',choices=['v1','v2'])
    parser.add_argument('--marker')
    a=parser.parse_args()
    old,marker=verify(a.instance)
    if a.command=='verify':
        print(f'PASS: HTTPD {a.instance}: {old} ({marker})')
        return
    if not a.version or not a.marker: abort('set requires --version and --marker')
    if a.version=='v1' and a.marker!='baseline-v1': abort('V1 needs baseline-v1 marker')
    if a.version=='v2' and not re.fullmatch(r'candidate-v2-[0-9]{8}T[0-9]{6}Z',a.marker):
        abort('Invalid V2 marker')
    p=SPECS[a.instance]
    data=p.read_text()
    data=replace_n(data,r'^        demo-rollout-revision: \S+$',
                   '        demo-rollout-revision: '+a.marker,1,'revision annotation')
    data=replace_n(data,r'^            name: httpd-page-v[12]$',
                   '            name: httpd-page-'+a.version,1,'page ConfigMap')
    data=replace_n(data,r'^                value: v[12]$',
                   '                value: '+a.version,5,'analysis args')
    p.write_text(data)
    verify(a.instance)
    print(f'Set HTTPD {a.instance} to {a.version} ({a.marker})')

if __name__=='__main__': main()
