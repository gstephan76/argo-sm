#!/usr/bin/env python3
"""Safely validate or switch the desired BLUE/GREEN image in the GitOps Rollout.

No Kubernetes or network access; stdlib only.
"""
import argparse
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / 'full-single-service' / 'rollout.yaml'
MANIFEST = ROOT / 'argocd' / 'application-full-single-service.yaml'
PARTS = ROOT / 'full-single-service'


def abort(message):
    raise SystemExit(f'ERROR: {message}')


def validate():
    files = re.findall(r'^  - ([a-z0-9-]+\.yaml)$',
                       (PARTS / 'kustomization.yaml').read_text(), re.M)
    if len(files) != 9 or len(files) != len(set(files)):
        abort('Expected 9 unique Kustomize resources')
    for name in files:
        if not (PARTS / name).is_file():
            abort(f'Missing Kustomize resource: {name}')
    app = MANIFEST.read_text()
    for expected in ('repoURL: https://github.com/gstephan76/argo-sm.git',
                     'path: full-single-service',
                     'namespace: full-single-service'):
        if expected not in app:
            abort(f'Argo CD Application mismatch: {expected}')
    content = SPEC.read_text()
    colors = re.findall(r'^[ \t]*image: argoproj/rollouts-demo:(blue|green)[ \t]*$', content, re.M)
    markers = re.findall(r'^\s*demo-rollout-revision: ([^\s]+)\s*$', content, re.M)
    expectations = re.findall(r'^[ \t]*value: (blue|green)[ \t]*$', content, re.M)
    if len(colors) != 1 or len(markers) != 1 or len(expectations) != 5:
        abort('Expected exactly one image, one revision marker and five analysis colors')
    if any(color != colors[0] for color in expectations):
        abort('Analysis expected colors do not match desired image')
    if colors[0] == 'blue' and markers[0] != 'baseline-blue':
        abort('BLUE revision must use canonical baseline-blue marker')
    if colors[0] == 'green' and not re.fullmatch(r'candidate-green-[0-9]{8}T[0-9]{6}Z', markers[0]):
        abort('GREEN revision must use a unique candidate-green timestamp marker')
    return colors[0], markers[0]


def replace_exact(content, pattern, replacement, amount, description):
    changed, n = re.subn(pattern, lambda _: replacement, content, flags=re.M)
    if n != amount:
        abort(f'{description}: expected {amount} matches; found {n}')
    return changed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=('verify', 'set'))
    parser.add_argument('--color', choices=('blue', 'green'))
    parser.add_argument('--marker')
    args = parser.parse_args()
    color, marker = validate()
    if args.command == 'verify':
        print(f'PASS: self-contained full-single-service manifests ({color}, {marker})')
        return
    if not args.color or not args.marker:
        abort('set requires --color and --marker')
    if args.color == 'blue' and args.marker != 'baseline-blue':
        abort('BLUE must use baseline-blue marker')
    if args.color == 'green' and not re.fullmatch(r'candidate-green-[0-9]{8}T[0-9]{6}Z', args.marker):
        abort('Invalid candidate-green marker')
    content = SPEC.read_text()
    content = replace_exact(content,
                            r'^[ \t]*demo-rollout-revision: [^\n]+$',
                            f'        demo-rollout-revision: {args.marker}',
                            1, 'revision annotation')
    content = replace_exact(content,
                            r'^[ \t]*image: argoproj/rollouts-demo:(blue|green)[ \t]*$',
                            f'          image: argoproj/rollouts-demo:{args.color}',
                            1, 'rollouts-demo image')
    content = replace_exact(content,
                            r'^[ \t]*value: (blue|green)[ \t]*$',
                            f'                value: {args.color}',
                            5, 'analysis colors')
    SPEC.write_text(content)
    validate()
    print(f'Set desired Rollout to {args.color} ({args.marker})')

if __name__ == '__main__':
    main()
