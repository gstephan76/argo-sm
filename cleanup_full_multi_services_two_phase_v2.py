#!/usr/bin/env python3
"""Two-phase, fail-closed migration to exactly two Services per full-multi Rollout.

MIGRATE: keep legacy anchors temporarily; update Backend/CRUD virtual hosts
         and Front/Backend upstream URLs to the retained track Services.
FINALIZE: after all 4 HTTP Rollouts have promoted and all old pods have
          drained, delete the 4 anchor Service manifests (GitOps prunes them).

Run from the repository root. Only --apply writes. --check previews offline.
The script never touches MariaDB Rollouts, workloads, Pods, or cluster RBAC.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

PHASE_KEY = 'SERVICE_CLEANUP_PHASE'
LEGACY = ('backend-a', 'backend-b', 'crud-a', 'crud-b')
ROOT_PHASE_MARKER = 'TIERS=(\'front\',\'backend\',\'mariadb\')'
VERIFY_ANCHOR = "    print('PASS: eight Rollouts, CRUD DB routing and observability contracts')"


def error(message):
    raise RuntimeError(message)


def text_at(root, rel):
    p = root / rel
    if not p.is_file():
        error(f'Missing expected file: {rel}')
    return p.read_text(encoding='utf-8')


def replace_exact(s, old, new, description, *, allow_new=True):
    if old == new:
        return s
    a, b = s.count(old), s.count(new)
    if a == 1 and b == 0:
        return s.replace(old, new, 1)
    if allow_new and a == 0 and b == 1:
        return s
    error(f'{description}: expected one old or one new anchor; found old={a}, new={b}')


def current_phase(spec):
    m = re.search(r'^SERVICE_CLEANUP_PHASE\s*=\s*[\'\"](migrate|finalize)[\'\"]$', spec, re.M)
    if m:
        return m.group(1)
    if PHASE_KEY in spec:
        error('Unexpected service cleanup phase marker')
    return 'baseline'


def migration_contract():
    return '''    # Verify a deliberate, bounded Service topology and updated in-mesh URLs.
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
            hosts_part = vs.split('  hosts:\\n', 1)[1].split('  gateways:', 1)[0]
            assert f'    - {new_host}\\n' in hosts_part, (tier, app, new_host)
            assert (f'    - {old_host}\\n' in hosts_part) == (SERVICE_CLEANUP_PHASE == 'migrate'), (tier, app, old_host)
'''.replace('\\n', '\\n')


def plan(root, stage):
    root = root.resolve()
    d = root / 'full-multi'
    sp = 'scripts/full-multi-spec.py'
    spec = text_at(root, sp)
    phase = current_phase(spec)
    if stage == 'migrate' and phase == 'finalize':
        error('Already finalized: refusing to restore deprecated Services')
    if stage == 'finalize' and phase == 'baseline':
        error('First run --stage migrate, commit/push and complete the HTTP Rollout promotions')

    changed = {}
    removed = []
    if stage == 'migrate':
        for app in 'ab':
            for tier, env_name, old_service, new_service, port in (
                ('front', 'BACKEND_API_URL', f'backend-{app}', f'backend-{app}-stable', 5000),
                ('backend', 'CRUD_API_URL', f'crud-{app}', f'crud-{app}-blue', 8080),
            ):
                rel = f'full-multi/rollout-{tier}-{app}.yaml'
                original = text_at(root, rel)
                old = f'value: "http://{old_service}.full-multi.svc.cluster.local:{port}/api/books"'
                new = f'value: "http://{new_service}.full-multi.svc.cluster.local:{port}/api/books"'
                if env_name not in original:
                    error(f'{rel}: {env_name} not present')
                result = replace_exact(original, old, new, rel)
                if result != original:
                    changed[rel] = result
            for tier, suffix in (('backend', 'stable'), ('crud', 'blue')):
                rel = f'full-multi/{tier}-{app}-virtualservice.yaml'
                original = text_at(root, rel)
                old = f'    - {tier}-{app}.full-multi.svc.cluster.local\n'
                new_host = f'    - {tier}-{app}-{suffix}.full-multi.svc.cluster.local\n'
                if new_host in original:
                    if old not in original:
                        error(f'{rel}: missing legacy host during migration stage')
                    result = original
                else:
                    if original.count(old) != 1:
                        error(f'{rel}: cannot find singular existing hosts entry')
                    result = original.replace(old, old + new_host, 1)
                if result != original:
                    changed[rel] = result
        if phase == 'baseline':
            if spec.count(ROOT_PHASE_MARKER) != 1 or spec.count(VERIFY_ANCHOR) != 1:
                error('full-multi-spec.py: cannot locate exact contract insertion anchors')
            spec = spec.replace(ROOT_PHASE_MARKER, ROOT_PHASE_MARKER + "\nSERVICE_CLEANUP_PHASE = 'migrate'", 1)
            spec = spec.replace(VERIFY_ANCHOR, migration_contract() + VERIFY_ANCHOR, 1)
        # Keep compatibility with the existing verifier. Replace precisely.
        old_check = "assert f'http://crud-{app}.full-multi.svc.cluster.local:8080/api/books' in b"
        new_check = "assert f'http://crud-{app}-blue.full-multi.svc.cluster.local:8080/api/books' in b"
        if spec.count(old_check) == 1:
            spec = spec.replace(old_check, new_check, 1)
        elif new_check not in spec:
            error('full-multi-spec.py: CRUD upstream verifier missing')
        if spec != text_at(root, sp):
            changed[sp] = spec
    else:
        if phase == 'migrate':
            for app in 'ab':
                for tier, suffix in (('backend', 'stable'), ('crud', 'blue')):
                    rel = f'full-multi/{tier}-{app}-virtualservice.yaml'
                    original = text_at(root, rel)
                    old = f'    - {tier}-{app}.full-multi.svc.cluster.local\n'
                    new_host = f'    - {tier}-{app}-{suffix}.full-multi.svc.cluster.local\n'
                    if old not in original or new_host not in original:
                        error(f'{rel}: migration hosts missing; cannot finalize')
                    if original.count(old) != 1 or original.count(new_host) != 1:
                        error(f'{rel}: unexpected hosts cardinality')
                    changed[rel] = original.replace(old, '', 1)
            if spec.count("SERVICE_CLEANUP_PHASE = 'migrate'") != 1:
                error('Service cleanup migration marker missing')
            spec_final = spec.replace("SERVICE_CLEANUP_PHASE = 'migrate'", "SERVICE_CLEANUP_PHASE = 'finalize'", 1)
            old_suffixes = "for suffix in ('', '-blue', '-green'):"
            new_suffixes = "for suffix in ('-blue', '-green'):"
            if spec_final.count(old_suffixes) != 1:
                error('full-multi-spec.py: CRUD Service contract cannot be finalized safely')
            spec_final = spec_final.replace(old_suffixes, new_suffixes, 1)
            changed[sp] = spec_final
            ks='full-multi/kustomization.yaml'
            k = text_at(root, ks)
            for name in LEGACY:
                entry=f'  - service-{name}.yaml\n'
                if k.count(entry)!=1:
                    error(f'{ks}: expected exactly one {entry.strip()} entry')
                k=k.replace(entry,'',1)
                rel=f'full-multi/service-{name}.yaml'
                if not (root/rel).is_file():
                    error(f'Missing {rel}; refusing incomplete finalization')
                removed.append(rel)
            changed[ks]=k
            readme='full-multi/README.md'
            crud_doc = 'full-multi/CRUD-TIER.md'
            if (root/crud_doc).exists():
                doc = (root/crud_doc).read_text(encoding='utf-8')
                old_description = 'The Backend forwards HTTP `/api/books` to the A/B CRUD anchor Service.'
                new_description = 'The Backend forwards HTTP `/api/books` to the `crud-{a,b}-blue` Service, which serves as the in-mesh routing host.'
                if old_description in doc:
                    changed[crud_doc] = doc.replace(old_description, new_description, 1)
            if (root/readme).exists():
                old=(root/readme).read_text(encoding='utf-8')
                # Repair the prominent manifest topology description if still present.
                old_lines=('Two backend anchor Services (`backend-a` and `backend-b`) provide\n'
                           'stable DNS names to which the internal Istio VirtualServices apply. Together\n'
                           'with the gateway Service and six CRUD Services, there are **21 Services** in the manifests.')
                new_lines=('The Backend VirtualServices use `backend-{a,b}-stable` as their mesh routing\n'
                           'host, and the CRUD VirtualServices use `crud-{a,b}-blue`. Each Rollout\n'
                           'has exactly two track Services; including the gateway, there are\n'
                           '**17 Services** in the manifests.')
                if old_lines in old:
                    changed[readme]=old.replace(old_lines,new_lines,1)
        elif phase == 'finalize':
            for name in LEGACY:
                rel=f'full-multi/service-{name}.yaml'
                if (root/rel).exists():
                    error(f'Finalized contract still has obsolete file {rel}')
        else:
            error(f'Unexpected phase {phase}')

    # Cross-resource assertions on the proposed tree, without modifying disk.
    new_spec = changed.get(sp, spec)
    expected_phase = 'migrate' if stage == 'migrate' else 'finalize'
    if current_phase(new_spec) != expected_phase:
        error('Resulting manifest verification phase is wrong')
    for app in 'ab':
        for tier, target, endpoint in (
            ('front',f'backend-{app}-stable',5000),
            ('backend',f'crud-{app}-blue',8080),
        ):
            rel=f'full-multi/rollout-{tier}-{app}.yaml'
            data=changed.get(rel, text_at(root, rel))
            if f'http://{target}.full-multi.svc.cluster.local:{endpoint}/api/books' not in data:
                error(f'{rel}: missing migrated upstream URL')
        for tier,suffix in (('backend','stable'),('crud','blue')):
            rel=f'full-multi/{tier}-{app}-virtualservice.yaml'
            data=changed.get(rel, text_at(root,rel))
            new=f'    - {tier}-{app}-{suffix}.full-multi.svc.cluster.local\n'
            old=f'    - {tier}-{app}.full-multi.svc.cluster.local\n'
            if data.count(new)!=1 or (old in data)!=(stage=='migrate'):
                error(f'{rel}: invalid resulting hosts')
    if stage == 'finalize':
        # No remaining manifest may depend on a Service DNS name being pruned.
        for rel_path in (root/'full-multi').glob('*.yaml'):
            rel=str(rel_path.relative_to(root))
            if rel in removed:
                continue
            body=changed.get(rel, rel_path.read_text(encoding='utf-8'))
            for legacy in LEGACY:
                if f'{legacy}.full-multi.svc.cluster.local' in body:
                    error(f'{rel}: still refers to deleted Service DNS {legacy}')
    return changed, removed, phase


def cmd(root, *parts):
    p=subprocess.run(parts,cwd=root,capture_output=True,text=True,timeout=45)
    if p.returncode:
        error(f'{" ".join(parts)} failed: {p.stderr.strip() or p.stdout.strip()}')
    return p.stdout.strip()


def safety_check_live(root):
    # Fail closed: stage 2 requires a committed, GitOps-synchronized migration,
    # healthy HTTP Rollouts, and no old pods still referencing legacy DNS names.
    if cmd(root, 'git','branch','--show-current') != 'main':
        error('Finalize requires main branch')
    if cmd(root,'git','diff','--name-only') or cmd(root,'git','diff','--cached','--name-only'):
        error('Finalize requires clean tracked files; commit/push migration first')
    sha=cmd(root, 'git','rev-parse','HEAD')
    argo=json.loads(cmd(root,'oc','-n','openshift-gitops','get','application','full-multi','-o','json'))
    state=argo.get('status',{}).get('sync',{})
    if state.get('revision')!=sha or state.get('status')!='Synced':
        error(f'Argo CD not Synced to local HEAD {sha[:12]}: got {state}')
    # Front A/B share a single gateway-scoped VirtualService; only Backend A/B
    # have per-instance, mesh-scoped VirtualServices. Do not query nonexistent
    # front-a-router / front-b-router resources.
    front_vs=json.loads(cmd(root,'oc','-n','full-multi','get','virtualservice',
                            'full-multi-front-router','-o','json'))
    front_http={route.get('name'): route for route in front_vs.get('spec',{}).get('http',[])}
    for app in 'ab':
        expected_front_routes={
            f'front-{app}-force-stable': {f'front-{app}-stable'},
            f'front-{app}-force-canary': {f'front-{app}-canary'},
            ('primary' if app=='a' else 'secondary'): {
                f'front-{app}-stable', f'front-{app}-canary'},
        }
        for route_name, destinations in expected_front_routes.items():
            route=front_http.get(route_name)
            if route is None:
                error(f'full-multi-front-router: missing route {route_name}')
            actual={r.get('destination',{}).get('host') for r in route.get('route',[])}
            if actual != destinations:
                error(f'full-multi-front-router/{route_name}: destination mismatch: {actual}')
        for tier in ('front','backend'):
            name=f'{tier}-{app}'
            obj=json.loads(cmd(root,'oc','-n','full-multi','get','rollouts.argoproj.io',name,'-o','json'))
            s=obj.get('status',{})
            if s.get('phase')!='Healthy' or not s.get('stableRS') or s.get('stableRS')!=s.get('currentPodHash'):
                error(f'{name}: not fully promoted/Healthy (phase={s.get("phase")}, stable={s.get("stableRS")}, current={s.get("currentPodHash")})')
        # The Backend routes need both the old and new authority hostnames
        # during migration. The old hostname is removed only at finalize.
        vs=json.loads(cmd(root,'oc','-n','full-multi','get','virtualservice',
                          f'backend-{app}-router','-o','json'))
        expected=(f'backend-{app}.full-multi.svc.cluster.local',
                  f'backend-{app}-stable.full-multi.svc.cluster.local')
        hosts=vs.get('spec',{}).get('hosts',[])
        if not all(host in hosts for host in expected):
            error(f'backend-{app}-router: dual migration hosts not live: {hosts}')
        http={r.get('name'): r for r in vs.get('spec',{}).get('http',[])}
        for route_name, destinations in {
            f'backend-{app}-force-stable': {f'backend-{app}-stable'},
            f'backend-{app}-force-canary': {f'backend-{app}-canary'},
            f'backend-{app}-default': {f'backend-{app}-stable',f'backend-{app}-canary'},
        }.items():
            route=http.get(route_name)
            if route is None:
                error(f'backend-{app}-router: missing route {route_name}')
            actual={r.get('destination',{}).get('host') for r in route.get('route',[])}
            if actual != destinations:
                error(f'backend-{app}-router/{route_name}: destination mismatch: {actual}')
        crud_vs=json.loads(cmd(root,'oc','-n','full-multi','get','virtualservice',
                               f'crud-{app}-router','-o','json'))
        expected=(f'crud-{app}.full-multi.svc.cluster.local',
                  f'crud-{app}-blue.full-multi.svc.cluster.local')
        hosts=crud_vs.get('spec',{}).get('hosts',[])
        if not all(host in hosts for host in expected):
            error(f'crud-{app}-router: dual migration hosts not live: {hosts}')
        http={r.get('name'): r for r in crud_vs.get('spec',{}).get('http',[])}
        for route_name,destination,header in (
            (f'crud-{app}-green',f'crud-{app}-green.full-multi.svc.cluster.local','canary'),
            (f'crud-{app}-blue-default',f'crud-{app}-blue.full-multi.svc.cluster.local','stable'),
        ):
            route=http.get(route_name)
            if route is None:
                error(f'crud-{app}-router: missing route {route_name}')
            actual={r.get('destination',{}).get('host') for r in route.get('route',[])}
            if actual!={destination}:
                error(f'crud-{app}-router/{route_name}: destination mismatch: {actual}')
            selected=(route.get('headers',{}).get('request',{}).get('set',{})
                      .get('x-crud-db-target'))
            if selected!=header:
                error(f'crud-{app}-router/{route_name}: database header mismatch: {selected}')
    pods=json.loads(cmd(root,'oc','-n','full-multi','get','pods','-o','json')).get('items',[])
    inspected=set()
    for pod in pods:
        name=pod.get('metadata',{}).get('name','?')
        app=pod.get('metadata',{}).get('labels',{}).get('app','')
        if app not in {f'{t}-{a}' for t in ('front','backend') for a in 'ab'}:
            continue
        if pod.get('status',{}).get('phase') in ('Succeeded','Failed'):
            continue
        # Even terminating old stable Pods must have drained before the DNS alias is pruned.
        key='BACKEND_API_URL' if app.startswith('front-') else 'CRUD_API_URL'
        a=app.rsplit('-',1)[1]
        dest=(f'backend-{a}-stable' if key=='BACKEND_API_URL' else f'crud-{a}-blue')
        port=5000 if key=='BACKEND_API_URL' else 8080
        expected=f'http://{dest}.full-multi.svc.cluster.local:{port}/api/books'
        vals=[e.get('value') for c in pod.get('spec',{}).get('containers',[])
              for e in c.get('env',[]) if e.get('name')==key]
        if vals != [expected]:
            error(f'{name}: {key}={vals!r} still refers to old/unknown target; wait for old pods to drain')
        inspected.add(app)
    required={f'{t}-{a}' for t in ('front','backend') for a in 'ab'}
    if inspected != required:
        error(f'Missing live migrated workloads: {required-inspected}')
    print('PASS live guards: Argo CD synced, 4 HTTP Rollouts healthy, all live pods use new DNS names')


def main():
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',default='.',type=Path)
    ap.add_argument('--stage',choices=('migrate','finalize'),required=True)
    g=ap.add_mutually_exclusive_group(required=True)
    g.add_argument('--check',action='store_true')
    g.add_argument('--apply',action='store_true')
    args=ap.parse_args()
    root=args.root.resolve()
    try:
        changed,removed,old_phase=plan(root,args.stage)
        print(f'Stage: {args.stage}; repository phase: {old_phase}')
        for path in sorted(changed):
            print(('WOULD UPDATE' if args.check else 'UPDATE')+' '+path)
        for path in sorted(removed):
            print(('WOULD REMOVE' if args.check else 'REMOVE')+' '+path)
        if not changed and not removed:
            print('Already applied: no changes')
            return 0
        if args.check:
            if args.stage=='finalize':
                print('NOTE: live cluster safety checks run only on --apply')
            return 0
        if args.stage == 'finalize':
            safety_check_live(root)
        # All expected content has been read and validated before the first write.
        for rel,data in changed.items():
            (root/rel).write_text(data,encoding='utf-8')
        for rel in removed:
            (root/rel).unlink()
        print('PASS stage',args.stage,'applied; review diff and validate before committing')
        return 0
    except (RuntimeError, OSError, ValueError, subprocess.TimeoutExpired) as e:
        print('ERROR:',e,file=sys.stderr)
        return 1


if __name__=='__main__':
    sys.exit(main())
