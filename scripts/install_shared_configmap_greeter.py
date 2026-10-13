#!/usr/bin/env python3
"""Install one shared Greeter image and two ConfigMap-backed app IDs in argo-sm.

Run from repository root; validate, build/push the image, THEN git commit/push.
No remote changes or cluster changes are made by this installer.
"""
from __future__ import annotations

import argparse
import ast
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent
# This installer is downloadable; locate repository from current working directory.
REPO = Path.cwd()

GO_MAIN = r'''package main

import (
    "fmt"
    "log"
    "net/http"
    "os"
    "regexp"
    "strings"
    "sync/atomic"
    "time"
)

var validID = regexp.MustCompile(`^[A-Za-z0-9_.-]{1,64}$`)

type greeter struct {
    prefix string
    instanceID string
    requests atomic.Uint64
}

func newGreeter(prefix, instanceID string) (http.Handler, error) {
    if !validID.MatchString(instanceID) {
        return nil, fmt.Errorf("GREETER_INSTANCE_ID must be 1..64 letters, digits, dots, dashes or underscores")
    }
    if !regexp.MustCompile(`^Application [AB] - (BLUE|YELLOW)$`).MatchString(prefix) {
        return nil, fmt.Errorf("MESSAGE_PREFIX must be Application A/B - BLUE/YELLOW")
    }
    g := &greeter{prefix: prefix, instanceID: instanceID}
    mux := http.NewServeMux()
    mux.HandleFunc("/", func(w http.ResponseWriter, r *http.Request) {
        if r.URL.Path != "/" { http.NotFound(w, r); return }
        if r.Method != http.MethodGet { w.WriteHeader(http.StatusMethodNotAllowed); return }
        w.Header().Set("Content-Type", "text/plain; charset=utf-8")
        fmt.Fprintf(w, "%s greeter => '%s' : %d\n", g.prefix, g.instanceID, g.requests.Add(1))
    })
    mux.HandleFunc("/healthz", func(w http.ResponseWriter, r *http.Request) {
        if r.Method != http.MethodGet { w.WriteHeader(http.StatusMethodNotAllowed); return }
        w.Header().Set("Content-Type", "text/plain; charset=utf-8")
        fmt.Fprintln(w, "OK")
    })
    return mux, nil
}

func main() {
    prefix := strings.TrimSpace(os.Getenv("MESSAGE_PREFIX"))
    id := strings.TrimSpace(os.Getenv("GREETER_INSTANCE_ID"))
    handler, err := newGreeter(prefix, id)
    if err != nil { log.Fatal(err) }
    server := &http.Server{
        Addr: ":8080",
        Handler: handler,
        ReadHeaderTimeout: 5 * time.Second,
        IdleTimeout: 60 * time.Second,
    }
    log.Printf("Greeter listening on :8080 (prefix=%s, ID=%s)", prefix, id)
    log.Fatal(server.ListenAndServe())
}
'''

GO_TEST = r'''package main

import (
    "io"
    "net/http"
    "net/http/httptest"
    "strings"
    "testing"
)

func request(t *testing.T, h http.Handler, path string) (int, string) {
    t.Helper()
    r := httptest.NewRequest(http.MethodGet, path, nil)
    w := httptest.NewRecorder()
    h.ServeHTTP(w, r)
    body, err := io.ReadAll(w.Result().Body)
    if err != nil { t.Fatal(err) }
    return w.Code, string(body)
}

func TestDistinctConfigMapIDsWithSameServer(t *testing.T) {
    a, err := newGreeter("Application A - BLUE", "greeter-a-8f1c4e")
    if err != nil { t.Fatal(err) }
    b, err := newGreeter("Application B - YELLOW", "greeter-b-3d7a92")
    if err != nil { t.Fatal(err) }
    for _, tc := range []struct{ h http.Handler; want string }{
        {a, "Application A - BLUE greeter => 'greeter-a-8f1c4e' : 1\n"},
        {b, "Application B - YELLOW greeter => 'greeter-b-3d7a92' : 1\n"},
        {a, "Application A - BLUE greeter => 'greeter-a-8f1c4e' : 2\n"},
    } {
        code, got := request(t, tc.h, "/")
        if code != 200 || got != tc.want { t.Fatalf("got %d %q; want %q", code, got, tc.want) }
    }
    if code, got := request(t, a, "/healthz"); code != 200 || got != "OK\n" { t.Fatalf("health: %d %q", code, got) }
    if code, _ := request(t, a, "/missing"); code != 404 { t.Fatalf("missing path: %d", code) }
}

func TestRefuseMissingOrInvalidIdentity(t *testing.T) {
    for _, id := range []string{"", "has a space", "has'quote", strings.Repeat("a", 65)} {
        if _, err := newGreeter("Application A - BLUE", id); err == nil { t.Fatalf("accepted %q", id) }
    }
}
'''

CONTAINERFILE = '''# One identical binary image for A and B; identity comes only from ConfigMaps.
FROM golang:1.25-alpine AS build
WORKDIR /src
COPY go.mod main.go main_test.go ./
RUN go test ./... && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /greeter .
FROM scratch
COPY --from=build /greeter /greeter
USER 10001:10001
EXPOSE 8080
ENTRYPOINT ["/greeter"]
'''

BUILD_SCRIPT = r'''#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="$(tr -d '\n' < "$ROOT/full-single-service/greeter-image.txt")"
[[ "$IMAGE" == *:configmap-v1 ]] || { echo "ERROR: expected a buildable image tag ending in :configmap-v1" >&2; exit 1; }
if command -v podman >/dev/null; then ENGINE=podman
elif command -v docker >/dev/null; then ENGINE=docker
else echo "ERROR: Install podman or docker" >&2; exit 1
fi
if [[ -n "${GHCR_TOKEN:-}" ]]; then
  printf '%s' "$GHCR_TOKEN" | "$ENGINE" login ghcr.io -u "${GHCR_USER:-gstephan76}" --password-stdin
fi
"$ENGINE" build -t "$IMAGE" -f "$ROOT/full-single-service/greeter/Containerfile" "$ROOT/full-single-service/greeter"
"$ENGINE" push "$IMAGE"
echo "Published $IMAGE"
echo "IMPORTANT: Set the GHCR package to PUBLIC (or configure an imagePullSecret) before pushing the GitOps Rollouts."
'''


def require_unique(s: str, old: str, new: str, file: str) -> str:
    n = s.count(old)
    if n != 1:
        raise ValueError(f"{file}: expected one anchor {old!r}, found {n}; refusing to edit")
    return s.replace(old, new, 1)


def get(p: str) -> str:
    f = REPO / p
    if not f.is_file():
        raise ValueError(f"Missing required repository file: {p}")
    return f.read_text(encoding="utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--image", default="ghcr.io/gstephan76/argo-sm-greeter:configmap-v1", help="Published image tag shared by both Rollouts")
    ap.add_argument("--a-id", default="greeter-a-8f1c4e")
    ap.add_argument("--b-id", default="greeter-b-3d7a92")
    args = ap.parse_args()
    image = args.image
    if not re.fullmatch(r"[a-z0-9][a-z0-9/._:-]{3,180}", image) or not image.endswith(":configmap-v1"):
        ap.error("Image must be a registry-qualified name with tag :configmap-v1")
    for label, value in (("a",args.a_id),("b",args.b_id)):
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", value):
            ap.error(f"--{label}-id must be 1..64 letters, digits, dots, underscores or dashes")
    if args.a_id == args.b_id:
        ap.error("A and B IDs must be distinct")
    if not (REPO/"full-single-service/rollout-a.yaml").exists() or not (REPO/"scripts/full-single-service-spec.py").exists():
        ap.error("Run from the gstephan76/argo-sm repository root")

    updates: dict[str,str] = {}
    creations: dict[str,str] = {}
    for app, identifier in (("a",args.a_id),("b",args.b_id)):
        path = f"full-single-service/rollout-{app}.yaml"
        data = get(path)
        data = require_unique(data,
            "          image: quay.io/rhdevelopers/knative-tutorial-greeter:quarkus\n",
            f"          image: {image}\n", path)
        data = require_unique(data, "app.kubernetes.io/name: knative-tutorial-greeter", "app.kubernetes.io/name: argo-sm-greeter", path)
        insertion = ("            - name: GREETER_INSTANCE_ID\n"
                     "              valueFrom:\n"
                     "                configMapKeyRef:\n"
                     f"                  name: full-single-service-{app}-identity\n"
                     "                  key: GREETER_INSTANCE_ID\n")
        data = require_unique(data,
            f'              value: "Application {app.upper()} - BLUE"\n',
            f'              value: "Application {app.upper()} - BLUE"\n'+insertion, path)
        updates[path] = data
        creations[f"full-single-service/configmap-{app}-identity.yaml"] = (
            "apiVersion: v1\nkind: ConfigMap\nmetadata:\n"
            f"  name: full-single-service-{app}-identity\n"
            "  namespace: full-single-service\n"
            "  annotations:\n    argocd.argoproj.io/sync-wave: '0'\n"
            f"data:\n  GREETER_INSTANCE_ID: '{identifier}'\n")

        path = f"full-single-service/analysis-template-{app}.yaml"
        data = get(path)
        env = ("                    env:\n"
               "                      - name: GREETER_INSTANCE_ID\n"
               "                        valueFrom:\n"
               "                          configMapKeyRef:\n"
               f"                            name: full-single-service-{app}-identity\n"
               "                            key: GREETER_INSTANCE_ID\n")
        data = require_unique(data, "                    imagePullPolicy: IfNotPresent\n", "                    imagePullPolicy: IfNotPresent\n"+env, path)
        exact = ("                          printf '%s\\n' \"$got\" | grep -Fq \"greeter => '$GREETER_INSTANCE_ID'\" || {\n"
                 "                            echo \"Unexpected Greeter instance ID in response: $got\" >&2\n"
                 "                            exit 1\n"
                 "                          }\n")
        data = require_unique(data,
             f'                          echo "Probe $n: $got"\n',
             exact+f'                          echo "Probe $n: $got"\n', path)
        updates[path] = data

    path="full-single-service/kustomization.yaml"
    data=get(path)
    data=require_unique(data,"  - service-a-stable.yaml\n", "  - configmap-a-identity.yaml\n  - service-a-stable.yaml\n",path)
    data=require_unique(data,"  - service-b-stable.yaml\n", "  - configmap-b-identity.yaml\n  - service-b-stable.yaml\n",path)
    updates[path]=data

    path="scripts/full-single-service-spec.py"
    data=get(path)
    data=require_unique(data, '"service-b-stable.yaml", "service-b-canary.yaml", "analysis-template-a.yaml",',
            '"service-b-stable.yaml", "service-b-canary.yaml", "configmap-a-identity.yaml", "configmap-b-identity.yaml", "analysis-template-a.yaml",', path)
    data=require_unique(data,'IMAGE = "quay.io/rhdevelopers/knative-tutorial-greeter:quarkus"',
                        'IMAGE = (PARTS / "greeter-image.txt").read_text(encoding="utf-8").strip()',path)
    anchor='    spec = SPECS[instance].read_text()\n'
    guard=(f'    identity = (PARTS / f"configmap-{{instance}}-identity.yaml").read_text()\n'
           '    if not re.search(r"^  GREETER_INSTANCE_ID: \'[A-Za-z0-9_.-]{1,64}\'$", identity, re.M):\n'
           '        abort(f"Invalid identity ConfigMap for {instance}")\n'
           '    if f"name: full-single-service-{instance}-identity" not in identity:\n'
           '        abort(f"Incorrect identity ConfigMap name for {instance}")\n'
           '    if f"name: full-single-service-{instance}-identity" not in (PARTS / f"analysis-template-{instance}.yaml").read_text():\n'
           '        abort(f"Analysis Job must verify identity for {instance}")\n')
    data=require_unique(data,anchor,guard+anchor,path)
    data=require_unique(data,'"name: MESSAGE_PREFIX",', '"name: MESSAGE_PREFIX", "name: GREETER_INSTANCE_ID", "configMapKeyRef:",',path)
    updates[path]=data

    path="full-single-service/README.md"
    data=get(path)
    old=("This is an Argo CD + Argo Rollouts + OpenShift Service Mesh demo in namespace\n"
        "`full-single-service`, **not a Knative Service**. Both Rollouts deploy the same\n"
        "prebuilt Quarkus greeter used in the Knative tutorial:\n\n"
        "`quay.io/rhdevelopers/knative-tutorial-greeter:quarkus`\n\n"
        "The greeter supports a `MESSAGE_PREFIX` environment variable and already returns\n"
        "a pod-local, incrementing request count. No custom counter server, image build,\n"
        "HTML page, or ConfigMap volume is needed.\n")
    new=("This is an Argo CD + Argo Rollouts + OpenShift Service Mesh demo in namespace\n"
         "`full-single-service`, **not a Knative Service**. Both Rollouts reference\n"
         "the **same container image**, defined in `greeter-image.txt`. The greeter is\n"
         "built from `greeter/` and implements the original plain-text greeter format.\n\n"
         "The response identifier is loaded at process startup from `GREETER_INSTANCE_ID`,\n"
         "supplied by `configmap-a-identity.yaml` or `configmap-b-identity.yaml`. The\n"
         "BLUE/YELLOW prefix remains controlled by each Rollout's `MESSAGE_PREFIX`, and\n"
         "each pod retains its own incrementing request counter. The image contains\n"
         "no A/B-specific identity.\n")
    data=require_unique(data,old,new,path)
    data=require_unique(data,
        "Exact whitespace and hostname formatting are owned by the upstream greeter. The\n"
        "`Application X - COLOR` prefix is supplied through `MESSAGE_PREFIX`. The count\n"
        "is maintained per pod: it resets if that pod is replaced and may jump from the\n"
        "point of view of a client when Istio routes requests between BLUE/YELLOW pods.\n"
        "Canary AnalysisJobs and any other HTTP callers also increment the counter.\n",
        "The identifier comes from the app-specific ConfigMap and stays constant across\n"
        "pod restarts. The `Application X - COLOR` prefix comes from `MESSAGE_PREFIX`.\n"
        "The counter is maintained per pod; count values can vary when traffic is routed\n"
        "between stable and candidate versions. Canary AnalysisJobs also increment it.\n",path)
    data=require_unique(data,"Application A - BLUE greeter => 'pod-a-blue' : 1",f"Application A - BLUE greeter => '{args.a_id}' : 1",path)
    data=require_unique(data,"Application A - BLUE greeter => 'pod-a-blue' : 2",f"Application A - BLUE greeter => '{args.a_id}' : 2",path)
    data=require_unique(data,"Application B - YELLOW greeter => 'pod-b-yellow' : 1",f"Application B - YELLOW greeter => '{args.b_id}' : 1",path)
    # Keep the later example and migration instructions consistent with the new image.
    data=require_unique(data,
        "Representative responses (pod ID and counter will vary):",
        "Representative responses (ConfigMap identifiers stay constant; counters vary):",path)
    data=require_unique(data,"Application A - BLUE greeter => 'pod-a' : 1",f"Application A - BLUE greeter => '{args.a_id}' : 1",path)
    data=require_unique(data,"Application A - BLUE greeter => 'pod-a' : 2",f"Application A - BLUE greeter => '{args.a_id}' : 2",path)
    data=require_unique(data,"Application B - BLUE greeter => 'pod-b' : 1",f"Application B - BLUE greeter => '{args.b_id}' : 1",path)
    data=require_unique(data,"Once both Quarkus Rollouts have Ready pods", "Once both Greeter Rollouts have Ready pods",path)
    marker="## One desired replica per application\n"
    howto=("## Build and publish the shared image (before syncing the Rollouts)\n\n"
           "From repository root, with podman or docker installed and authenticated to GHCR:\n\n"
           "```bash\n"
           "# Optional: export GHCR_TOKEN from your private environment (never commit it).\n"
           "# This script runs registry login only if GHCR_TOKEN is supplied.\n"
           "bash scripts/build-push-full-single-service-greeter.sh\n"
           "```\n\n"
           "The published package must be public for these Rollouts to pull it without\n"
           "an imagePullSecret. Alternatively, configure imagePullSecrets in both\n"
           "Rollouts before deploying. **Build and push before committing the manifests**:\n"
           "Argo CD automatically synchronizes `main` and would otherwise reference\n"
           "an image that does not exist. Use a new immutable image tag for any future\n"
           "source change. `greeter-image.txt` is the image reference verified for both\n"
           "Rollouts by `scripts/full-single-service-spec.py`.\n\n"
           "You can edit the two identity ConfigMaps without building a new image.\n"
           "Environment variables are captured when a pod starts, so a ConfigMap edit\n"
           "requires replacing the pod (normally by a new GitOps Rollout revision)\n"
           "before the response identifier changes. During a BLUE/YELLOW rollout,\n"
           "stable and candidate pods of the same application use the same identity.\n\n")
    data=require_unique(data,marker,howto+marker,path)
    updates[path]=data

    creations.update({
        "full-single-service/greeter/main.go":GO_MAIN,
        "full-single-service/greeter/main_test.go":GO_TEST,
        "full-single-service/greeter/go.mod":"module example.com/argo-sm/greeter\n\ngo 1.23\n",
        "full-single-service/greeter/Containerfile":CONTAINERFILE,
        "full-single-service/greeter-image.txt":image+"\n",
        "scripts/build-push-full-single-service-greeter.sh":BUILD_SCRIPT,
    })
    # Refuse partial or repeated installations; no file is modified until every check passes.
    for f in creations:
        if (REPO/f).exists():
            raise ValueError(f"Refusing to overwrite already existing {f}")
    for f,body in updates.items():
        if f.endswith(".py"):
            ast.parse(body, filename=f)
    for f,body in {**updates,**creations}.items():
        target=REPO/f
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body,encoding="utf-8")
    (REPO/"scripts/build-push-full-single-service-greeter.sh").chmod(0o755)
    print(f"Installed shared Greeter {image} with ConfigMap IDs A={args.a_id} B={args.b_id}")
    print("Next: run Go tests, build and push image, ensure registry visibility, then commit/push Git changes.")
    return 0

if __name__=="__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, SyntaxError) as exc:
        print(f"ERROR: {exc}",file=sys.stderr)
        raise SystemExit(1)
