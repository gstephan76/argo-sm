#!/usr/bin/env python3
"""Install visible BLUE/YELLOW Frontend+Backend revision evidence for argo-sm.

Rebased on upstream GitHub main 84595c250f38a8b9fba54cd8e9bc802388c8ca37.
Two GitOps phases, without touching MariaDB/CRUD/Services/VirtualServices:
  install   Source, tests, immutable image tag, and BLUE HTTP baseline manifests.
  candidate Create a YELLOW candidate for only the four HTTP Front/Backend Rollouts.

Check previews edits without writes; apply writes local files only. Never publishes,
commits, pushes, or mutates an OpenShift cluster.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
from pathlib import Path
import re
import sys

OLD_IMAGE = 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-crud-v3'
NEW_IMAGE = 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-release-theme-v2'
MAIN = 'full-multi/bookshelf/main.go'
TEST = 'full-multi/bookshelf/main_test.go'
DOC = 'full-multi/RELEASE-THEME.md'

NEW_BOOKS_BLOCK = '''\tif s.role == "front" || s.role == "backend" {
\t\tb, status, err := s.upstream(r)
\t\tif err != nil {
\t\t\thttp.Error(w, "downstream unreachable: "+err.Error(), http.StatusBadGateway)
\t\t\treturn
\t\t}
\t\tif status == http.StatusOK {
\t\t\tvar out response
\t\t\tif err := json.Unmarshal(b, &out); err != nil {
\t\t\t\thttp.Error(w, "invalid downstream response", http.StatusBadGateway)
\t\t\t\treturn
\t\t\t}
\t\t\tif s.role == "backend" {
\t\t\t\tout.Backend = s.prefix
\t\t\t\tout.BackendRelease = releaseColor(s.prefix)
\t\t\t\tout.BackendPod = s.hostname
\t\t\t} else {
\t\t\t\tout.Frontend = s.prefix
\t\t\t\tout.FrontendRelease = releaseColor(s.prefix)
\t\t\t\tout.FrontendPod = s.hostname
\t\t\t}
\t\t\tb, err = json.Marshal(out)
\t\t\tif err != nil {
\t\t\t\thttp.Error(w, "cannot encode downstream response", http.StatusInternalServerError)
\t\t\t\treturn
\t\t\t}
\t\t\tsetReleaseHeaders(w.Header(), out)
\t\t}
\t\tw.Header().Set("Content-Type", "application/json")
\t\tw.WriteHeader(status)
\t\t_, _ = w.Write(b)
\t\treturn
\t}
'''

TEMPLATE = r'''var indexTemplate = template.Must(template.New("books").Parse(`<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Bookshelf {{.App}} — Release Dashboard</title>
<style>
:root{font-family:system-ui,-apple-system,sans-serif;color:#12233b;background:#f3f6fb}
*{box-sizing:border-box}body{margin:0}main{max-width:1040px;margin:0 auto;padding:32px 22px 70px}
h1{margin:0;font-size:clamp(29px,4vw,42px);letter-spacing:-.04em}h2{margin:.15rem 0 .6rem;font-size:clamp(25px,3vw,35px)}
.subtitle{color:#53627b;margin:8px 0 28px}.lanes{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:18px}
.card{border-radius:20px;padding:26px;min-width:0;box-shadow:0 14px 35px #14274612;border:1px solid #dbe2ed;background:#fff}
.card.blue{--accent:#1755c9;--surface:#e7efff;--ink:#123782}
.card.yellow{--accent:#d08b00;--surface:#fff5d8;--ink:#6a4800}
.card.unknown{--accent:#626b7b;--surface:#eef0f3;--ink:#384152}
.hero{display:flex;align-items:center;gap:16px;padding:16px;border-radius:14px;background:var(--surface);color:var(--accent)}
.hero svg{width:64px;height:64px;flex:none;stroke:currentColor;fill:none;stroke-width:2.8;stroke-linecap:round;stroke-linejoin:round}
.pill{display:inline-block;font-weight:800;font-size:.8rem;letter-spacing:.06em;border-radius:100px;padding:7px 13px;background:var(--accent);color:white}
.role{font-weight:700;font-size:.85rem;letter-spacing:.12em;text-transform:uppercase;color:#53627b}
.identity{font-family:ui-monospace,monospace;overflow-wrap:anywhere;font-size:.8rem;color:#53627b}
.flow{font-size:24px;font-weight:800;letter-spacing:.1em;text-align:center;margin:16px;color:#72809a}
.database{margin-top:20px;background:#fff;border:1px solid #dbe2ed;border-radius:16px;padding:18px}
.database code,.help code{background:#eff2f8;padding:4px 6px;border-radius:5px;overflow-wrap:anywhere}
.tablewrap{overflow-x:auto}.books{width:100%;border-collapse:collapse;background:#fff;border-radius:14px;overflow:hidden}
.books td,.books th{text-align:left;padding:11px 16px;border-bottom:1px solid #e7ebf1}.books th{background:#e8eef9}
.help{margin-top:16px;color:#53627b;font-size:.85rem;line-height:1.8}
@media(max-width:650px){.lanes{grid-template-columns:1fr}main{padding:22px 14px}.card{padding:18px}}
</style></head><body><main>
<h1>Bookshelf {{.App}} <span aria-label="application">📚</span></h1>
<p class="subtitle">Actual running revisions, independently observed at Frontend and Backend. BLUE/YELLOW is the revision color, not a promise about current stable/canary Service ownership.</p>
<div class="lanes">
<section class="card {{.FrontendClass}}" aria-label="Frontend revision"><div class="role">Frontend</div>
<div class="hero"><svg viewBox="0 0 80 80" role="img" aria-label="Frontend monitor illustration"><rect x="7" y="12" width="66" height="43" rx="6"/><path d="M29 67h22M40 55v12"/><path d="M21 26h38M21 35h24M21 43h16"/></svg>
<div><span class="pill">{{.FrontendRelease}}</span><h2>Frontend</h2></div></div>
<p class="identity">{{.Frontend}}</p><div class="identity">Pod: {{.FrontendPod}}</div></section>
<section class="card {{.BackendClass}}" aria-label="Backend revision"><div class="role">Backend</div>
<div class="hero"><svg viewBox="0 0 80 80" role="img" aria-label="Backend server illustration"><rect x="12" y="12" width="56" height="17" rx="4"/><rect x="12" y="33" width="56" height="17" rx="4"/><rect x="12" y="54" width="56" height="17" rx="4"/><path d="M23 20h2M23 41h2M23 62h2M37 20h21M37 41h21M37 62h21"/></svg>
<div><span class="pill">{{.BackendRelease}}</span><h2>Backend</h2></div></div>
<p class="identity">{{.Backend}}</p><div class="identity">Pod: {{.BackendPod}}</div></section>
</div>
<div class="flow" aria-hidden="true">↓</div>
<section class="database"><strong>MariaDB selected route:</strong> <code>{{.DatabaseTrack}}</code> via <code>{{.DatabaseHost}}</code></section>
<h2>Literature collection</h2><div class="tablewrap"><table class="books"><thead><tr><th>Title</th><th>Author</th><th>Year</th></tr></thead><tbody>
{{range .Books}}<tr><td>{{.Title}}</td><td>{{.Author}}</td><td>{{.Year}}</td></tr>{{end}}
</tbody></table></div><p class="help">Routing headers: <code>x-app-target: a|b</code>, <code>x-front-track: stable|canary</code>, <code>x-backend-track: stable|canary</code>, <code>x-db-track: blue|green</code>.<br>API evidence: <code>/api/books</code>, headers <code>X-Bookshelf-Frontend-Release</code> and <code>X-Bookshelf-Backend-Release</code>.</p>
</main></body></html>`))
'''

INDEX_FUNC = '''func (s *server) index(w http.ResponseWriter, r *http.Request) {
\tif r.URL.Path != "/" {
\t\thttp.NotFound(w, r)
\t\treturn
\t}
\tif s.role != "front" {
\t\thttp.NotFound(w, r)
\t\treturn
\t}
\tb, status, err := s.upstream(r)
\tif err != nil {
\t\thttp.Error(w, "backend unavailable", http.StatusBadGateway)
\t\treturn
\t}
\tif status != http.StatusOK {
\t\thttp.Error(w, "backend returned "+strconv.Itoa(status), http.StatusBadGateway)
\t\treturn
\t}
\tvar out response
\tif err := json.Unmarshal(b, &out); err != nil {
\t\thttp.Error(w, "invalid backend response", http.StatusBadGateway)
\t\treturn
\t}
\tout.Frontend = s.prefix
\tout.FrontendRelease = releaseColor(s.prefix)
\tout.FrontendPod = s.hostname
\tsetReleaseHeaders(w.Header(), out)
\tw.Header().Set("Content-Type", "text/html; charset=utf-8")
\tclassFor := func(release string) string {
\t\tswitch release {
\t\tcase "BLUE": return "blue"
\t\tcase "YELLOW": return "yellow"
\t\tdefault: return "unknown"
\t\t}
\t}
\tif err := indexTemplate.Execute(w, struct {
\t\tApp, Frontend, FrontendRelease, FrontendPod, FrontendClass string
\t\tBackend, BackendRelease, BackendPod, BackendClass string
\t\tDatabaseTrack, DatabaseHost string
\t\tBooks []book
\t}{
\t\tApp: s.app, Frontend: out.Frontend, FrontendRelease: out.FrontendRelease,
\t\tFrontendPod: out.FrontendPod, FrontendClass: classFor(out.FrontendRelease),
\t\tBackend: out.Backend, BackendRelease: out.BackendRelease,
\t\tBackendPod: out.BackendPod, BackendClass: classFor(out.BackendRelease),
\t\tDatabaseTrack: out.DatabaseTrack, DatabaseHost: out.DatabaseHost, Books: out.Books,
\t}); err != nil {
\t\tlog.Printf("render bookshelf dashboard: %v", err)
\t}
}
'''

TEST_APPEND = '''
func TestReleaseColor(t *testing.T) {
\tfor _, tt := range []struct{ prefix, expected string }{
\t\t{"Application A - BLUE", "BLUE"},
\t\t{"Application B - YELLOW", "YELLOW"},
\t\t{"Application A - CRUD", "UNKNOWN"},
\t} {
\t\tif got := releaseColor(tt.prefix); got != tt.expected { t.Fatalf("%q: %q", tt.prefix, got) }
\t}
}

// The actual serving instances stamp their own identities, never infer
// identity from untrusted x-front-track / x-backend-track request headers.
func TestFrontAndBackendReleaseEvidence(t *testing.T) {
\tcrud := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
\t\tif r.Header.Get("x-db-track") != "green" { t.Errorf("CRUD lost DB routing header") }
\t\t_ = json.NewEncoder(w).Encode(response{App:"A", DatabaseTrack:"canary",
\t\t\tDatabaseHost:"mariadb-a-canary.full-multi.svc.cluster.local",
\t\t\tBooks:[]book{{ID:1, Title:"Dom Casmurro", Author:"Machado de Assis",Year:1899}}})
\t}))
\tdefer crud.Close()
\tbackend := &server{role:"backend", app:"A", prefix:"Application A - YELLOW", hostname:"backend-canary-pod", crudURL:crud.URL+"/api/books",client:crud.Client()}
\tbe := httptest.NewServer(backend.routes())
\tdefer be.Close()
\tfront := &server{role:"front", app:"A", prefix:"Application A - BLUE", hostname:"front-stable-pod",backendURL:be.URL+"/api/books",client:be.Client()}
\tfor _, path := range []string{"/api/books", "/"} {
\t\tr := httptest.NewRequest(http.MethodGet,path,nil)
\t\tr.Header.Set("x-db-track","green")
\t\tr.Header.Set("x-front-track","canary") // deliberately wrong: serving Frontend is BLUE
\t\tr.Header.Set("x-backend-track","stable") // deliberately wrong: serving Backend is YELLOW
\t\tw := httptest.NewRecorder()
\t\tfront.routes().ServeHTTP(w,r)
\t\tif w.Code != http.StatusOK { t.Fatalf("%s: %d %s",path,w.Code,w.Body.String()) }
\t\tif got := w.Header().Get("X-Bookshelf-Frontend-Release"); got != "BLUE" { t.Fatalf("front header=%q",got) }
\t\tif got := w.Header().Get("X-Bookshelf-Backend-Release"); got != "YELLOW" { t.Fatalf("backend header=%q",got) }
\t\tif path == "/" {
\t\t\tfor _, want := range []string{"Frontend", "Backend", "front-stable-pod", "backend-canary-pod", "Dom Casmurro", "class=\\"card blue\\"", "class=\\"card yellow\\""} {
\t\t\t\tif !strings.Contains(w.Body.String(),want) { t.Fatalf("HTML missing %q",want) }
\t\t\t}
\t\t\tcontinue
\t\t}
\t\tvar got response
\t\tif err := json.Unmarshal(w.Body.Bytes(), &got);err != nil {t.Fatal(err)}
\t\tif got.FrontendRelease != "BLUE" || got.BackendRelease != "YELLOW" || got.FrontendPod != "front-stable-pod" || got.BackendPod != "backend-canary-pod" || got.DatabaseTrack != "canary" {t.Fatalf("JSON: %+v",got)}
\t}
}
'''

DOC_CONTENT = '''# Full-multi: Frontend and Backend release identity

The Bookshelf executable now reports independent evidence from the **actual**
Frontend and Backend processes, including release color and pod hostname.
Its HTML homepage has two illustrated, color-coded release panels.

BLUE and YELLOW describe **application revision content**, not permanent
Kubernetes Service roles. After promotion, a YELLOW release can become the
stable Service target. Test stable vs canary simultaneously only while both
ReplicaSets have ready pods (e.g. a paused 50/50 HTTP canary rollout).

The patch uses one shared immutable executable image, configurable with
`MESSAGE_PREFIX`, so both front and backend can exhibit either release.
The CRUD Rollouts remain on their existing image; MariaDB and all routing
Services/VirtualServices are untouched. No changes to SQL data or traces.

## Build and deployment order

1. Run the installer with `--phase install --check`, then `--apply`.
2. Validate Go tests and GitOps manifests, and **build/push the new immutable
   image before any Git commit**:

   ```bash
   bash scripts/build-push-full-multi.sh
   skopeo inspect --no-creds docker://ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-release-theme-v2
   ```
3. Commit/push the files changed by `install` to `main`; Argo CD auto-syncs.
   The four HTTP Rollouts create a new BLUE baseline candidate. Only after
   their smoke analyses pass, manually promote **Frontend and Backend only**.
4. Confirm all four HTTP Rollouts are Healthy and the Argo CD Application is
   Synced; run the installer with `--phase candidate --check` then `--apply`.
   Commit/push the **four HTTP Rollout manifests only**. After Argo CD sync,
   leave them paused at 50%: BLUE stable versus YELLOW canary.
5. Do **not** promote either MariaDB during this sequence. Their BLUE/GREEN
   demo datasets and emptyDir lifecycles are independent.

## API and page smoke test

```bash
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
for ft in stable canary; do
  for bt in stable canary; do
    echo "FRONT=$ft / BACKEND=$bt"
    curl -kfsS -D /tmp/bookshelf-headers.txt \\
      -H 'x-app-target: a' -H "x-front-track: $ft" \\
      -H "x-backend-track: $bt" -H 'x-db-track: blue' \\
      "https://${HOST}/api/books" | jq '{frontend_release,frontend_pod,backend_release,backend_pod,database_track,database_host}'
    grep -i '^x-bookshelf-.*release:' /tmp/bookshelf-headers.txt
  done
done
```

Open `https://${HOST}/` (without forced headers this follows Istio weights),
or use `curl -kfsS -H ... https://${HOST}/` to view the HTML.
A web browser cannot set arbitrary request headers itself; use a request-header
extension or the API, or let the weighted routes demonstrate both revisions.

All previous `/api/books` fields (including `backend`, `database_track`,
`database_host`, `books`) remain backward-compatible. `/info` remains unchanged
for analysis Jobs. No new Services or virtual hosts are created.
'''


def replace_once(value: str, old: str, new: str, path: str) -> str:
    n = value.count(old)
    if n != 1:
        raise RuntimeError(f'{path}: expected exactly one anchor {old[:85]!r}, got {n}; stopping safely')
    return value.replace(old, new, 1)


def install_main(src: str) -> str:
    if 'FrontendRelease string `json:"frontend_release,omitempty"`' in src:
        return src
    path=MAIN
    src=replace_once(src,
        '\tBackend       string `json:"backend,omitempty"`\n',
        '\tBackend       string `json:"backend,omitempty"`\n'
        '\tFrontend      string `json:"frontend,omitempty"`\n'
        '\tFrontendRelease string `json:"frontend_release,omitempty"`\n'
        '\tFrontendPod   string `json:"frontend_pod,omitempty"`\n'
        '\tBackendRelease string `json:"backend_release,omitempty"`\n'
        '\tBackendPod    string `json:"backend_pod,omitempty"`\n',path)
    src=replace_once(src,
        'role, app, prefix, backendURL, crudURL, dbStable, dbCanary, dbName, dbUser, dbPassword string',
        'role, app, prefix, hostname, backendURL, crudURL, dbStable, dbCanary, dbName, dbUser, dbPassword string',path)
    src=replace_once(src,'prefix: os.Getenv("MESSAGE_PREFIX"), backendURL:',
        'prefix: os.Getenv("MESSAGE_PREFIX"), hostname: os.Getenv("HOSTNAME"), backendURL:',path)
    src=replace_once(src,'func newServer() *server {', '''func releaseColor(prefix string) string {
\tif strings.HasSuffix(prefix, " - BLUE") { return "BLUE" }
\tif strings.HasSuffix(prefix, " - YELLOW") { return "YELLOW" }
\treturn "UNKNOWN"
}

func setReleaseHeaders(h http.Header, out response) {
\tif out.FrontendRelease != "" { h.Set("X-Bookshelf-Frontend-Release", out.FrontendRelease) }
\tif out.BackendRelease != "" { h.Set("X-Bookshelf-Backend-Release", out.BackendRelease) }
}

func newServer() *server {''',path)
    start=src.index('\tif s.role == "front" || s.role == "backend" {',src.index('func (s *server) books('))
    end=src.index('\tif s.role != "crud" {',start)
    src=src[:start]+NEW_BOOKS_BLOCK+src[end:]
    start=src.index('var indexTemplate =')
    end=src.index('func main() {',start)
    src=src[:start]+TEMPLATE+'\n'+INDEX_FUNC+'\n'+src[end:]
    return src


def install_test(src: str) -> str:
    if 'func TestFrontAndBackendReleaseEvidence(' in src: return src
    if 'func TestBackendForwardsToCRUD(' not in src: raise RuntimeError(f'{TEST}: missing expected prior CRUD tests')
    return src+'\n'+TEST_APPEND


def install_spec(src: str) -> str:
    old="assert 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-crud-v3' in s"
    new=f"assert '{NEW_IMAGE}' in s"
    if new in src: return src
    # Rebased against gstephan76/argo-sm@84595c250f38a8b9fba54cd8e9bc802388c8ca37.
    # Exactly ONE literal check covers the four HTTP Frontend/Backend Rollouts
    # inside the verification loop; CRUD has its own separately named `r` check.
    # Never change the CRUD image requirement.
    if src.count(old)!=1:
        raise RuntimeError(f'scripts/full-multi-spec.py: expected exactly one HTTP v3 image check, found {src.count(old)}')
    crud_check = "assert 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-crud-v3' in r"
    if src.count(crud_check)!=1:
        raise RuntimeError('scripts/full-multi-spec.py: missing separate CRUD v3 image check')
    updated=src.replace(old,new,1)
    if new not in updated or crud_check not in updated:
        raise RuntimeError('scripts/full-multi-spec.py: unexpected image-check transformation')
    return updated


def install_builder(src: str) -> str:
    if NEW_IMAGE in src: return src
    return replace_once(src,OLD_IMAGE,NEW_IMAGE,'scripts/build-push-full-multi.sh')


def phase_rollout(src: str, app: str, phase: str) -> str:
    # Only front/backend Rollouts: do not touch MariaDB or CRUD Pod templates.
    name=app.upper()
    out=src
    oldimg=src.count(OLD_IMAGE)
    newimg=src.count(NEW_IMAGE)
    if phase == 'install':
        if 'candidate-yellow-release-theme-v2' in out:
            raise RuntimeError('already in YELLOW candidate phase; do not reset it with --phase install')
        if oldimg==1 and newimg==0: out=out.replace(OLD_IMAGE,NEW_IMAGE)
        elif oldimg==0 and newimg==1: pass
        else: raise RuntimeError('unexpected Front/Backend image reference')
        # Install BLUE baseline even when the currently committed revision is YELLOW.
        out=re.sub(r'(?m)^(        demo-rollout-revision: )\S+$',r'\g<1>baseline-blue-release-theme-v2',out,count=1)
        out=re.sub(rf'(              value: "Application {name} - )(BLUE|YELLOW)(")',r'\g<1>BLUE\3',out,count=1)
        if 'value: "Application '+name+' - BLUE"' not in out:raise RuntimeError('could not install BLUE MESSAGE_PREFIX')
        out=re.sub(r'(?m)^(                value: )(BLUE|YELLOW)$',r'\g<1>BLUE',out)
        return out
    if oldimg or newimg != 1: raise RuntimeError('publish/install image and BLUE baseline before creating YELLOW candidate')
    if 'candidate-yellow-release-theme-v2' in out and f'Application {name} - YELLOW' in out:
        return out  # idempotent second invocation
    if 'baseline-blue-release-theme-v2' not in out:
        raise RuntimeError('expected release-identity BLUE baseline before candidate')
    marker='candidate-yellow-release-theme-v2-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out=re.sub(r'(?m)^(        demo-rollout-revision: )\S+$',lambda m:m[1]+marker,out,count=1)
    out=re.sub(rf'(              value: "Application {name} - )(BLUE|YELLOW)(")',r'\g<1>YELLOW\3',out,count=1)
    out=re.sub(r'(?m)^(                value: )(BLUE|YELLOW)$',r'\g<1>YELLOW',out)
    return out


def patch(root: Path, phase: str) -> dict[str, tuple[str,str]]:
    wanted={}
    def change(p: str, transform):
        f=root/p
        if not f.is_file():raise RuntimeError(f'missing {p}')
        old=f.read_text(encoding='utf-8'); new=transform(old)
        if old!=new:wanted[p]=(old,new)
    if phase=='install':
        change(MAIN,install_main)
        change(TEST,install_test)
        change('scripts/full-multi-spec.py',install_spec)
        change('scripts/build-push-full-multi.sh',install_builder)
        f=root/DOC
        if f.exists():
            if f.read_text(encoding='utf-8')!=DOC_CONTENT:raise RuntimeError(f'Refusing to overwrite customized {DOC}')
        else:wanted[DOC]=('',DOC_CONTENT)
    for app in 'ab':
        for tier in ('front','backend'):
            path=f'full-multi/rollout-{tier}-{app}.yaml'
            change(path,lambda s,a=app:phase_rollout(s,a,phase))
    return wanted


def main() -> int:
    ap=argparse.ArgumentParser(description=__doc__)
    ap.add_argument('--root',type=Path,default=Path('.'))
    ap.add_argument('--phase',choices=['install','candidate'],required=True)
    action=ap.add_mutually_exclusive_group(required=True)
    action.add_argument('--check',action='store_true')
    action.add_argument('--apply',action='store_true')
    args=ap.parse_args()
    try:
        changes=patch(args.root.resolve(),args.phase)
        for path,(old,new) in changes.items():
            print(('UPDATE ' if args.apply else 'WOULD UPDATE ')+path)
            if args.apply:
                target=args.root.resolve()/path
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_text(new,encoding='utf-8')
        print(f'PASS {args.phase}: {len(changes)} file(s) '+('updated' if args.apply else 'would change'))
        return 0
    except (OSError,RuntimeError,ValueError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr)
        return 2



# v2: release theme is a mounted ConfigMap, re-read per request.
# The base patch above installs the independent Frontend/Backend release evidence;
# this enhancement introduces user-configurable appearance without altering routing.
CONFIGMAP = 'full-multi/configmap-release-theme.yaml'
CONFIGMAP_CONTENT = '''apiVersion: v1
kind: ConfigMap
metadata:
  name: bookshelf-release-theme
  namespace: full-multi
  annotations:
    argocd.argoproj.io/sync-wave: '-1'
data:
  theme.json: |
    {
      "blue": {
        "label": "BLUE RELEASE",
        "subtitle": "Baseline software revision",
        "accent": "#1755c9",
        "surface": "#e7efff",
        "ink": "#123782"
      },
      "yellow": {
        "label": "YELLOW RELEASE",
        "subtitle": "Candidate software revision",
        "accent": "#d08b00",
        "surface": "#fff5d8",
        "ink": "#6a4800"
      }
    }
'''

THEME_GO = r'''// Release color is an observed executable revision; colors and display labels
// are supplied by the projected ConfigMap, not by the incoming routing headers.
type releaseStyle struct {
    Label string `json:"label"`
    Subtitle string `json:"subtitle"`
    Accent string `json:"accent"`
    Surface string `json:"surface"`
    Ink string `json:"ink"`
}
type releaseTheme struct {
    Blue releaseStyle `json:"blue"`
    Yellow releaseStyle `json:"yellow"`
}
func defaultTheme() releaseTheme {
    return releaseTheme{
        Blue: releaseStyle{Label:"BLUE RELEASE",Subtitle:"Baseline software revision",Accent:"#1755c9",Surface:"#e7efff",Ink:"#123782"},
        Yellow: releaseStyle{Label:"YELLOW RELEASE",Subtitle:"Candidate software revision",Accent:"#d08b00",Surface:"#fff5d8",Ink:"#6a4800"},
    }
}
func validHexColor(s string) bool {
    if len(s)!=7 || s[0]!='#' {return false}
    for _, c := range s[1:] {
        if !((c>='0' && c<='9') || (c>='a' && c<='f') || (c>='A' && c<='F')) {return false}
    }
    return true
}
func mergeStyle(defaults, supplied releaseStyle) releaseStyle {
    if supplied.Label!="" && len(supplied.Label)<=80 {defaults.Label=supplied.Label}
    if supplied.Subtitle!="" && len(supplied.Subtitle)<=200 {defaults.Subtitle=supplied.Subtitle}
    if validHexColor(supplied.Accent) {defaults.Accent=supplied.Accent}
    if validHexColor(supplied.Surface) {defaults.Surface=supplied.Surface}
    if validHexColor(supplied.Ink) {defaults.Ink=supplied.Ink}
    return defaults
}
func (s *server) theme() releaseTheme {
    defaults:=defaultTheme()
    path:=s.themeFile
    if path=="" {path="/etc/bookshelf-theme/theme.json"}
    // Projected ConfigMap volumes update atomically; do not cache at startup.
    stat,err:=os.Stat(path)
    if err!=nil || stat.Size()>16*1024 {return defaults}
    data,err:=os.ReadFile(path)
    if err!=nil {return defaults}
    var cfg releaseTheme
    if json.Unmarshal(data,&cfg)!=nil {return defaults}
    defaults.Blue=mergeStyle(defaults.Blue,cfg.Blue)
    defaults.Yellow=mergeStyle(defaults.Yellow,cfg.Yellow)
    return defaults
}
func (t releaseTheme) forRelease(release string) releaseStyle {
    switch release {
        case "BLUE": return t.Blue
        case "YELLOW": return t.Yellow
        default: return releaseStyle{Label:"UNKNOWN", Subtitle:"Unrecognized revision",Accent:"#626b7b",Surface:"#eef0f3",Ink:"#384152"}
    }
}
func (t releaseTheme) css() template.CSS {
    // CSS-safe conversion: only strict #RRGGBB values are ever interpolated.
    return template.CSS(fmt.Sprintf(".card.blue{--accent:%s;--surface:%s;--ink:%s}.card.yellow{--accent:%s;--surface:%s;--ink:%s}",
        t.Blue.Accent,t.Blue.Surface,t.Blue.Ink,t.Yellow.Accent,t.Yellow.Surface,t.Yellow.Ink))
}
'''

THEME_TEST_GO = r'''
func TestConfigMapThemeLiveReload(t *testing.T) {
    path:=filepath.Join(t.TempDir(),"theme.json")
    s:=&server{themeFile:path}
    if s.theme().Blue.Label!="BLUE RELEASE" {t.Fatal("default theme missing")}
    initial:=[]byte(`{"blue":{"label":"CUSTOM BLUE","accent":"#113355","surface":"#ddeeff","ink":"#112233"}}`)
    if err:=os.WriteFile(path,initial,0600);err!=nil{t.Fatal(err)}
    if s.theme().Blue.Label!="CUSTOM BLUE" || s.theme().Blue.Accent!="#113355" {t.Fatal("ConfigMap not read")}
    modified:=[]byte(`{"blue":{"label":"UPDATED BLUE","accent":"url(javascript:alert(1))"}}`)
    if err:=os.WriteFile(path,modified,0600);err!=nil{t.Fatal(err)}
    if s.theme().Blue.Label!="UPDATED BLUE" {t.Fatal("ConfigMap live update not visible")}
    if s.theme().Blue.Accent!="#1755c9" {t.Fatal("unsafe CSS was accepted")}
}
'''

THEME_DOC = '''# ConfigMap-driven release identity and styling

The `bookshelf-release-theme` ConfigMap contains `theme.json` with two
independent palettes, `blue` and `yellow`. Each supports `label`, `subtitle`,
`accent`, `surface`, and `ink`. Change these values in
`full-multi/configmap-release-theme.yaml` and push through GitOps to update
both HTTP tiers **without a new image**. Kubelet projects the updated file;
the Go application reads it for each request (do not use `subPath`). Only
strict #RRGGBB color strings are accepted; malformed data uses defaults.

The actual Frontend/Backend release identity is stamped by each serving pod,
not inferred from request headers. BLUE/YELLOW describes executable revision,
not a permanent stable/canary Service ownership. After promotion, a YELLOW
revision can be stable. `/api/books` includes frontend/backend release, pod,
label, accent, plus the existing database response. The web page presents
two illustrated, color-coded cards and the database selection. `/info` and
Argo Rollouts analysis contracts remain unchanged.

## Deploy BLUE baseline, then YELLOW candidate

From the repository root:

```
python3 update_full_multi_release_theme_v2.py --phase install --check
python3 update_full_multi_release_theme_v2.py --phase install --apply
python3 scripts/full-multi-spec.py verify
oc kustomize full-multi >/dev/null
(cd full-multi/bookshelf && go test ./...)
bash scripts/build-push-full-multi.sh
skopeo inspect --no-creds docker://ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-release-theme-v2
```

Build/push the new immutable image **before** pushing the GitOps manifests.
Commit/push the installer-applied changes. After Argo CD is synced and the four
HTTP Rollouts pass their analysis/pause, promote Frontend and Backend only.
Once all four are Healthy and all old pods drain, run the installer with
`--phase candidate --check` and `--apply`, commit/push only the four HTTP
Rollout manifests, and leave them paused at 50%. MariaDB is untouched.

## Routing verification

```
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
for ft in stable canary; do
  for bt in stable canary; do
    echo "FRONT=$ft / BACKEND=$bt"
    curl -kfsS -H 'x-app-target: a' -H "x-front-track: $ft" \\
      -H "x-backend-track: $bt" -H 'x-db-track: blue' \\
      "https://${HOST}/api/books" | \\
      jq '{frontend_release,frontend_pod,frontend_label,frontend_accent,backend_release,backend_pod,backend_label,backend_accent,database_host}'
  done
done
```

Browser homepage: `https://${HOST}/` (weighted by Istio when no track headers
are supplied). A browser address bar cannot set custom headers; use curl or a
header-extension to force combinations. The existing 17 Services, Istio routing,
tracing context, CRUD and MariaDB images remain unchanged.
'''

def upgrade_main_with_theme(src: str) -> str:
    # Only called after the v1 identity patch has produced the full source.
    if 'type releaseTheme struct' in src: return src
    src=replace_once(src,'\tBackendPod    string `json:"backend_pod,omitempty"`\n',
        '\tBackendPod    string `json:"backend_pod,omitempty"`\n'
        '\tFrontendLabel string `json:"frontend_label,omitempty"`\n'
        '\tFrontendAccent string `json:"frontend_accent,omitempty"`\n'
        '\tBackendLabel string `json:"backend_label,omitempty"`\n'
        '\tBackendAccent string `json:"backend_accent,omitempty"`\n',MAIN)
    src=replace_once(src,
        'role, app, prefix, hostname, backendURL, crudURL, dbStable, dbCanary, dbName, dbUser, dbPassword string',
        'role, app, prefix, hostname, themeFile, backendURL, crudURL, dbStable, dbCanary, dbName, dbUser, dbPassword string',MAIN)
    src=replace_once(src,'hostname: os.Getenv("HOSTNAME"), backendURL:',
        'hostname: os.Getenv("HOSTNAME"), themeFile: os.Getenv("BOOKSHELF_THEME_FILE"), backendURL:',MAIN)
    src=replace_once(src,'func releaseColor(prefix string) string {',THEME_GO+'\nfunc releaseColor(prefix string) string {',MAIN)
    src=replace_once(src,'\tif out.BackendRelease != "" { h.Set("X-Bookshelf-Backend-Release", out.BackendRelease) }',
        '\tif out.BackendRelease != "" { h.Set("X-Bookshelf-Backend-Release", out.BackendRelease) }\n'
        '\tif out.FrontendAccent != "" { h.Set("X-Bookshelf-Frontend-Color", out.FrontendAccent) }\n'
        '\tif out.BackendAccent != "" { h.Set("X-Bookshelf-Backend-Color", out.BackendAccent) }',MAIN)
    src=replace_once(src,
        '\t\t\t\tout.BackendPod = s.hostname',
        '\t\t\t\tout.BackendPod = s.hostname\n'
        '\t\t\t\tstyle:=s.theme().forRelease(out.BackendRelease)\n'
        '\t\t\t\tout.BackendLabel=style.Label\n\t\t\t\tout.BackendAccent=style.Accent',MAIN)
    src=replace_once(src,
        '\t\t\t\tout.FrontendPod = s.hostname',
        '\t\t\t\tout.FrontendPod = s.hostname\n'
        '\t\t\t\tstyle:=s.theme().forRelease(out.FrontendRelease)\n'
        '\t\t\t\tout.FrontendLabel=style.Label\n\t\t\t\tout.FrontendAccent=style.Accent',MAIN)
    src=replace_once(src,
        '.card.blue{--accent:#1755c9;--surface:#e7efff;--ink:#123782}\n.card.yellow{--accent:#d08b00;--surface:#fff5d8;--ink:#6a4800}',
        '{{.ThemeCSS}}',MAIN)
    src=replace_once(src,'.role{font-weight:700;',
        '.tagline{font-size:.88rem;color:var(--ink);font-weight:650;margin-top:12px}\n.role{font-weight:700;',MAIN)
    src=replace_once(src,'<span class="pill">{{.FrontendRelease}}</span><h2>Frontend</h2>',
        '<span class="pill">{{.FrontendLabel}}</span><h2>Frontend</h2>',MAIN)
    src=replace_once(src,'<span class="pill">{{.BackendRelease}}</span><h2>Backend</h2>',
        '<span class="pill">{{.BackendLabel}}</span><h2>Backend</h2>',MAIN)
    src=replace_once(src,'<p class="identity">{{.Frontend}}</p>',
        '<p class="tagline">{{.FrontendSubtitle}}</p><p class="identity">{{.Frontend}}</p>',MAIN)
    src=replace_once(src,'<p class="identity">{{.Backend}}</p>',
        '<p class="tagline">{{.BackendSubtitle}}</p><p class="identity">{{.Backend}}</p>',MAIN)
    src=replace_once(src,
        '\tout.FrontendPod = s.hostname\n\tsetReleaseHeaders(w.Header(), out)',
        '\tout.FrontendPod = s.hostname\n'
        '\tpalette:=s.theme()\n'
        '\tfrontStyle:=palette.forRelease(out.FrontendRelease)\n'
        '\tbackendStyle:=palette.forRelease(out.BackendRelease)\n'
        '\tout.FrontendLabel=frontStyle.Label\n'
        '\tout.FrontendAccent=frontStyle.Accent\n'
        '\tif out.BackendLabel=="" {out.BackendLabel=backendStyle.Label}\n'
        '\tif out.BackendAccent=="" {out.BackendAccent=backendStyle.Accent}\n'
        '\tsetReleaseHeaders(w.Header(), out)',MAIN)
    src=replace_once(src,'\t\tDatabaseTrack, DatabaseHost string\n\t\tBooks []book',
        '\t\tDatabaseTrack, DatabaseHost string\n'
        '\t\tFrontendLabel, FrontendSubtitle, BackendLabel, BackendSubtitle string\n'
        '\t\tThemeCSS template.CSS\n\t\tBooks []book',MAIN)
    src=replace_once(src,
        '\t\tDatabaseTrack: out.DatabaseTrack, DatabaseHost: out.DatabaseHost, Books: out.Books,',
        '\t\tDatabaseTrack: out.DatabaseTrack, DatabaseHost: out.DatabaseHost, Books: out.Books,\n'
        '\t\tFrontendLabel: out.FrontendLabel,FrontendSubtitle:frontStyle.Subtitle,\n'
        '\t\tBackendLabel: out.BackendLabel,BackendSubtitle:backendStyle.Subtitle,\n'
        '\t\tThemeCSS:palette.css(),',MAIN)
    return src

def upgrade_test_with_theme(src: str) -> str:
    if 'func TestConfigMapThemeLiveReload' in src:return src
    src=replace_once(src,'\t"strings"\n','\t"strings"\n\t"os"\n\t"path/filepath"\n',TEST)
    src=replace_once(src,'"Frontend", "Backend", "front-stable-pod"',
        '"Frontend", "Backend", "BLUE RELEASE", "YELLOW RELEASE", "front-stable-pod"',TEST)
    src=replace_once(src,'if got.FrontendRelease != "BLUE" || got.BackendRelease != "YELLOW"',
        'if got.FrontendLabel != "BLUE RELEASE" || got.BackendLabel != "YELLOW RELEASE" || got.FrontendAccent != "#1755c9" || got.BackendAccent != "#d08b00" || got.FrontendRelease != "BLUE" || got.BackendRelease != "YELLOW"',TEST)
    return src+'\n'+THEME_TEST_GO

def add_mount(src: str, path: str) -> str:
    if 'name: release-theme' in src:
        assert 'mountPath: /etc/bookshelf-theme' in src, path
        return src
    src=replace_once(src,'      containers:\n',
        '      volumes:\n        - name: release-theme\n          configMap:\n            name: bookshelf-release-theme\n      containers:\n',path)
    src=replace_once(src,'          imagePullPolicy: IfNotPresent\n          env:',
        '          imagePullPolicy: IfNotPresent\n          volumeMounts:\n            - name: release-theme\n              mountPath: /etc/bookshelf-theme\n              readOnly: true\n          env:',path)
    return src

DOC_CONTENT = THEME_DOC  # Preserve idempotence of the base installer.
BASE_PATCH = patch

def patch(root: Path, phase: str) -> dict[str, tuple[str,str]]:
    changes=BASE_PATCH(root,phase)
    if phase != 'install':
        return changes
    # Modify new document+code entirely in memory. The installer remains a
    # dry-run unless --apply and never contacts OpenShift or GitHub.
    for path, upgrade in ((MAIN,upgrade_main_with_theme),(TEST,upgrade_test_with_theme)):
        if path in changes:
            old,new=changes[path]
            changes[path]=(old,upgrade(new))
    if DOC in changes:
        old,_=changes[DOC]
        changes[DOC]=(old,THEME_DOC)
    for app in 'ab':
        for tier in ('front','backend'):
            path=f'full-multi/rollout-{tier}-{app}.yaml'
            if path in changes:
                old,new=changes[path]
                changes[path]=(old,add_mount(new,path))
    cust='full-multi/kustomization.yaml'
    old=(root/cust).read_text(encoding='utf-8')
    new=old if '  - configmap-release-theme.yaml\n' in old else replace_once(old,'resources:\n','resources:\n  - configmap-release-theme.yaml\n',cust)
    if old!=new:changes[cust]=(old,new)
    cfg=root/CONFIGMAP
    if cfg.exists():
        if cfg.read_text(encoding='utf-8')!=CONFIGMAP_CONTENT:
            raise RuntimeError(f'Refusing to overwrite existing customized {CONFIGMAP}')
    else:changes[CONFIGMAP]=('',CONFIGMAP_CONTENT)
    return changes

if __name__=='__main__':raise SystemExit(main())
