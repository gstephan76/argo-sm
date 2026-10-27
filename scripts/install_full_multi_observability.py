#!/usr/bin/env python3
"""Idempotent, narrowly scoped installer for full-multi observability.

Run after extracting this package at the argo-sm repository root.
Modifies only main.go, main_test.go, kustomization.yaml, full-multi-spec.py,
build-push-full-multi.sh, and four Front/Backend Rollout image references.
Preserves all BLUE/YELLOW markers, weights and replica settings.
"""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
TAG_OLD = 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-v1'
TAG_NEW = 'ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-tracing-v2'

HEADERS = '''// Istio/Envoy requires the application to forward incoming trace context
// when it creates a new downstream HTTP request. Do not forward credentials.
var downstreamTraceHeaders = []string{
    "x-request-id", "traceparent", "tracestate", "baggage",
    "b3", "x-b3-traceid", "x-b3-spanid", "x-b3-parentspanid",
    "x-b3-sampled", "x-b3-flags", "x-ot-span-context",
}

func propagateTraceHeaders(dst, src http.Header) {
    for _, name := range downstreamTraceHeaders {
        for _, value := range src.Values(name) {
            dst.Add(name, value)
        }
    }
}

'''

TEST = '''func TestPropagateTraceHeaders(t *testing.T) {
    src := http.Header{}
    src.Set("Traceparent", "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")
    src.Set("Tracestate", "vendor=value")
    src.Set("X-Request-Id", "req-123")
    src.Set("X-B3-Traceid", "aaaabbbbccccdddd")
    src.Set("B3", "aaaabbbbccccdddd-1234123412341234-1")
    src.Add("Baggage", "demo=a")
    src.Set("Authorization", "Bearer should-not-forward")
    dst := http.Header{}
    propagateTraceHeaders(dst, src)
    for _, name := range []string{"Traceparent", "Tracestate", "X-Request-Id", "X-B3-Traceid", "B3", "Baggage"} {
        if dst.Get(name) != src.Get(name) { t.Errorf("%s not forwarded: %q", name, dst.Get(name)) }
    }
    if dst.Get("Authorization") != "" { t.Fatal("credentials must not be copied") }
}

'''

changes = {}

def plan(path, old, new):
    p = ROOT / path
    if not p.is_file():
        raise RuntimeError(f'Missing expected file: {path}')
    s = changes.get(p, p.read_text())
    if new in s:
        print(f'Already applied: {path}')
        return
    if old not in s:
        raise RuntimeError(f'Unexpected contents in {path}; refusing partial patch (missing {old[:70]!r})')
    if s.count(old) != 1:
        raise RuntimeError(f'Ambiguous patch: {path} has {s.count(old)} occurrences')
    changes[p] = s.replace(old, new, 1)

try:
    p = 'full-multi/bookshelf/main.go'
    if 'func propagateTraceHeaders(dst, src http.Header)' not in (ROOT/p).read_text():
        plan(p, 'func (s *server) upstream(r *http.Request) ([]byte, int, error) {',
             HEADERS + 'func (s *server) upstream(r *http.Request) ([]byte, int, error) {')
    if 'propagateTraceHeaders(req.Header, r.Header)' not in changes.get(ROOT/p, (ROOT/p).read_text()):
        plan(p, 'for _, h := range []string{"x-backend-track", "x-db-track"} {',
             'propagateTraceHeaders(req.Header, r.Header)\n\tfor _, h := range []string{"x-backend-track", "x-db-track"} {')
    p = 'full-multi/bookshelf/main_test.go'
    if 'func TestPropagateTraceHeaders(t *testing.T)' not in (ROOT/p).read_text():
        plan(p, 'func TestTracks(t *testing.T) {', TEST + 'func TestTracks(t *testing.T) {')
    plan('full-multi/kustomization.yaml', '  - telemetry.yaml\n',
         '  - telemetry.yaml\n  - podmonitor-istio-proxies.yaml\n')
    plan('scripts/full-multi-spec.py', TAG_OLD, TAG_NEW)
    plan('scripts/full-multi-spec.py', "for name in ('x-backend-track','x-db-track','DB_HOST_CANARY','DB_HOST_STABLE'):",
         "for name in ('x-backend-track','x-db-track','DB_HOST_CANARY','DB_HOST_STABLE','traceparent','propagateTraceHeaders'):")
    plan('scripts/full-multi-spec.py', "    print('PASS: exactly six Rollouts, twelve track Services, isolated A/B tier and header routing contracts')",
         "    assert 'otel-tracing' in (D/'telemetry.yaml').read_text()\n    assert (D/'podmonitor-istio-proxies.yaml').is_file()\n    print('PASS: six Rollouts, routing and observability contracts')")
    plan('scripts/build-push-full-multi.sh', TAG_OLD, TAG_NEW)
    for tier in ('front','backend'):
        for instance in 'ab':
            plan(f'full-multi/rollout-{tier}-{instance}.yaml', TAG_OLD, TAG_NEW)
    # Require additions from the package before touching existing files.
    for name in ('full-multi/podmonitor-istio-proxies.yaml',
                 'full-multi/OBSERVABILITY.md', 'scripts/full-multi-kiali-tempo.py',
                 'scripts/check-full-multi-tracing.sh'):
        if not (ROOT/name).is_file():
            raise RuntimeError(f'Missing package addition {name}; unzip the full package first')
    # Require all files to be present and write only after preflight succeeds.
    for path, content in changes.items():
        path.write_text(content)
        print('UPDATED', path.relative_to(ROOT))
    print('PASS: full-multi observability patch installed')
    print('NEXT: gofmt -w full-multi/bookshelf/main.go full-multi/bookshelf/main_test.go')
    print('NEXT: run go test, publish full-multi-tracing-v2 image, commit and push')
except RuntimeError as e:
    sys.exit('ERROR: ' + str(e))
