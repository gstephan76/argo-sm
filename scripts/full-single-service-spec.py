#!/usr/bin/env python3
"""Offline validation and safe BLUE/YELLOW editing for the A/B Quarkus greeters."""
import argparse
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parent.parent
PARTS = ROOT / "full-single-service"
SPECS = {i: PARTS / f"rollout-{i}.yaml" for i in ("a", "b")}
MANIFEST = ROOT / "argocd" / "application-full-single-service.yaml"
FILES = {
    "namespace.yaml", "ingress-gateway.yaml", "gateway.yaml", "route.yaml",
    "virtualservice.yaml", "service-a-stable.yaml", "service-a-canary.yaml",
    "service-b-stable.yaml", "service-b-canary.yaml", "configmap-a-identity.yaml", "configmap-b-identity.yaml", "analysis-template-a.yaml",
    "analysis-template-b.yaml", "rollout-a.yaml", "rollout-b.yaml", "podmonitor-istio-proxies.yaml",
}
IMAGE = (PARTS / "greeter-image.txt").read_text(encoding="utf-8").strip()


def abort(msg):
    raise SystemExit("ERROR: " + msg)


def replace_exact(text, pattern, replacement, total, label):
    new, found = re.subn(pattern, lambda m: replacement, text, flags=re.M)
    if found != total:
        abort(f"{label}: expected {total}, found {found}")
    return new


def verify(instance):
    listed = re.findall(r"^  - ([a-z0-9-]+\.yaml)$",
                        (PARTS / "kustomization.yaml").read_text(), re.M)
    if set(listed) != FILES or len(listed) != len(FILES):
        abort("Kustomization mismatch")
    for file in FILES:
        if not (PARTS / file).is_file():
            abort("Missing Kustomization resource: " + file)
    for retired in ("configmap-httpd-v1.yaml", "configmap-httpd-v2.yaml", "configmap-counter-server.yaml", "route-a.yaml", "route-b.yaml"):
        if (PARTS / retired).exists():
            abort("Retired ConfigMap still tracked: " + retired)
    public_route = (PARTS / "route.yaml").read_text()
    if not re.search(r"^metadata:\n  name: full-single-service\n", public_route, re.M):
        abort("Public Route must have the neutral name full-single-service")
    if not re.search(r"^    name: full-single-service-ingressgateway$", public_route, re.M):
        abort("Public Route must target shared ingressgateway")
    app = MANIFEST.read_text()
    for fragment in ("repoURL: https://github.com/gstephan76/argo-sm.git",
                     "path: full-single-service", "namespace: full-single-service"):
        if fragment not in app:
            abort("Argo CD mismatch: " + fragment)
    routing = (PARTS / "virtualservice.yaml").read_text()
    for fragment in ("name: primary", "name: secondary",
                     "host: full-single-service-a-stable", "host: full-single-service-a-canary",
                     "host: full-single-service-b-stable", "host: full-single-service-b-canary"):
        if fragment not in routing: abort("VirtualService mismatch: " + fragment)
    # One OpenShift Route: B's explicit header match must precede A's catch-all.
    if not (routing.index("    - name: secondary\n") < routing.index("    - name: primary\n")):
        abort("Secondary B route must precede primary default A route")
    secondary = routing.split("    - name: secondary\n", 1)[1].split("    - name: primary\n", 1)[0]
    primary = routing.split("    - name: primary\n", 1)[1]
    for fragment in ("x-app-target:", "exact: b", "host: full-single-service-b-stable", "host: full-single-service-b-canary"):
        if fragment not in secondary: abort("B header selection missing: " + fragment)
    for fragment in ("uri:", "prefix: /", "host: full-single-service-a-stable", "host: full-single-service-a-canary"):
        if fragment not in primary: abort("A default selection missing: " + fragment)
    if "x-app-target:" in primary or "x-app-target:" in (PARTS / "route.yaml").read_text():
        abort("A default route or OpenShift Route unexpectedly match selection header")
    identity = (PARTS / f"configmap-{instance}-identity.yaml").read_text()
    if not re.search(r"^  GREETER_INSTANCE_ID: '[A-Za-z0-9_.-]{1,64}'$", identity, re.M):
        abort(f"Invalid identity ConfigMap for {instance}")
    if f"name: full-single-service-{instance}-identity" not in identity:
        abort(f"Incorrect identity ConfigMap name for {instance}")
    if f"name: full-single-service-{instance}-identity" not in (PARTS / f"analysis-template-{instance}.yaml").read_text():
        abort(f"Analysis Job must verify identity for {instance}")
    spec = SPECS[instance].read_text()
    letter = instance.upper()
    for fragment in (f"name: full-single-service-{instance}\n", "  replicas: 1\n",
                     f"image: {IMAGE}", "name: MESSAGE_PREFIX", "name: GREETER_INSTANCE_ID", "configMapKeyRef:",
                     "tcpSocket:", "maxSurge: 1", "maxUnavailable: 0"):
        if fragment not in spec: abort(f"Missing {instance} Rollout contract: {fragment}")
    if spec.count("tcpSocket:") != 2 or "httpGet:" in spec or "volumeMounts:" in spec:
        abort("Probes must be TCP and no ConfigMap volume may remain")
    prefixes = re.findall(r'^              value: "(Application [AB] - (?:BLUE|YELLOW))"$', spec, re.M)
    markers = re.findall(r'^        demo-rollout-revision: (\S+)$', spec, re.M)
    gate_colors = re.findall(r'^                value: (BLUE|YELLOW)$', spec, re.M)
    if len(prefixes) != 1 or len(markers) != 1 or len(gate_colors) != 5:
        abort("Must have exactly one MESSAGE_PREFIX, marker, and five analysis colors")
    prefix = prefixes[0]
    if not prefix.startswith(f"Application {letter} - "):
        abort("Wrong application identifier in MESSAGE_PREFIX")
    color = prefix.split(" - ", 1)[1]
    if any(x != color for x in gate_colors):
        abort("Canary analysis colors do not match MESSAGE_PREFIX")
    marker = markers[0]
    if color == "BLUE" and marker != "baseline-blue":
        abort("BLUE marker must be baseline-blue")
    if color == "YELLOW" and not re.fullmatch(r'candidate-yellow-[0-9]{8}T[0-9]{6}Z', marker):
        abort("YELLOW marker must be timestamped")
    analysis = (PARTS / f"analysis-template-{instance}.yaml").read_text()
    if analysis.count('while [ "$n" -le 15 ]; do') != 1 or analysis.count("sleep 1") != 1:
        abort(f"AnalysisTemplate {instance} must perform 15 probes with one-second spacing")
    for fragment in ("name: expected-color", f"Application {letter}",
                     "candidate-greeting-and-counter", "[0-9]+$"):
        if fragment not in analysis:
            abort("Missing AnalysisTemplate requirement: " + fragment)
    return color, marker


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('verify', 'set'))
    parser.add_argument('--instance', choices=('a', 'b'), default='a')
    parser.add_argument('--color', choices=('blue', 'yellow'))
    parser.add_argument('--marker')
    args = parser.parse_args()
    old, marker = verify(args.instance)
    if args.command == 'verify':
        print(f'PASS: Application {args.instance.upper()}: {old} ({marker}), replicas=1')
        return
    if not args.color or not args.marker:
        abort('set requires --color and --marker')
    color = args.color.upper()
    if color == 'BLUE' and args.marker != 'baseline-blue':
        abort('BLUE marker must be baseline-blue')
    if color == 'YELLOW' and not re.fullmatch(r'candidate-yellow-[0-9]{8}T[0-9]{6}Z', args.marker):
        abort('Invalid YELLOW marker')
    p = SPECS[args.instance]
    data = p.read_text()
    data = replace_exact(data, r'^        demo-rollout-revision: \S+$',
                         '        demo-rollout-revision: '+args.marker, 1, 'revision marker')
    data = replace_exact(data, r'^              value: "Application [AB] - (?:BLUE|YELLOW)"$',
                         f'              value: "Application {args.instance.upper()} - {color}"', 1, 'message prefix')
    data = replace_exact(data, r'^                value: (BLUE|YELLOW)$',
                         '                value: '+color, 5, 'analysis args')
    p.write_text(data)
    verify(args.instance)
    print(f'Set Application {args.instance.upper()} to {color} ({args.marker})')

if __name__ == '__main__':
    main()
