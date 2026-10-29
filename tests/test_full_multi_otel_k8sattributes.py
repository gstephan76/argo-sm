#!/usr/bin/env python3
"""Offline fail-closed contract tests; no OpenShift API access."""
import copy
import importlib.util
import json
import pathlib
import unittest
from unittest import mock

SRC = pathlib.Path(__file__).resolve().parent.parent / 'scripts/full-multi-otel-k8sattributes.py'
spec = importlib.util.spec_from_file_location('otel_patch', SRC)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def baseline():
    return {
        'metadata': {'resourceVersion': '10844191', 'generation': 1},
        'spec': {
            'serviceAccount': 'otel-collector',
            'config': {
                'processors': {'memory_limiter': {'limit_mib': 400}, 'batch': {'timeout': '5s'}},
                'receivers': {'otlp': {}},
                'exporters': {'otlp_grpc/tempo': {'endpoint': 'tempo-mesh-gateway.tempo.svc.cluster.local:8090'}},
                'service': {'pipelines': {'traces': {
                    'receivers': ['otlp'], 'processors': ['memory_limiter', 'batch'],
                    'exporters': ['otlp_grpc/tempo']
                }}},
            }
        }
    }


class TestPatch(unittest.TestCase):
    def test_baseline_passes_and_patch_only_adds_processor(self):
        cr = baseline()
        with mock.patch.object(mod, 'read_resource', return_value=cr):
            got, state = mod.collector_snapshot()
        self.assertEqual(state, 'baseline')
        p = json.loads(mod.json_patch(got, True))
        self.assertEqual(p[0]['op'], 'test')
        self.assertEqual(p[0]['path'], '/metadata/resourceVersion')
        self.assertEqual(p[1]['value'], ['memory_limiter', 'batch'])
        self.assertEqual(p[2]['path'], '/spec/config/processors/k8sattributes')
        self.assertEqual(p[3]['path'], '/spec/config/service/pipelines/traces/processors/1')
        self.assertEqual(p[3]['value'], 'k8sattributes')

    def test_post_apply_recognized(self):
        cr = baseline()
        cr['spec']['config']['processors']['k8sattributes'] = copy.deepcopy(mod.PROCESSOR)
        cr['spec']['config']['service']['pipelines']['traces']['processors'] = list(mod.NEW_PROCESSORS)
        with mock.patch.object(mod, 'read_resource', return_value=cr):
            _, state = mod.collector_snapshot()
        self.assertEqual(state, 'enriched')
        p = json.loads(mod.json_patch(cr, False))
        self.assertEqual(p[-1], {'op': 'remove', 'path': '/spec/config/processors/k8sattributes'})

    def test_refuses_drift(self):
        for mutation in [
            lambda cr: cr['spec']['config']['service']['pipelines']['traces'].update({'processors': ['memory_limiter', 'filter', 'batch']}),
            lambda cr: cr['spec'].update({'serviceAccount': 'other-collector'}),
            lambda cr: cr['spec']['config']['exporters']['otlp_grpc/tempo'].update({'endpoint': 'other:4317'}),
        ]:
            cr = baseline()
            mutation(cr)
            with mock.patch.object(mod, 'read_resource', return_value=cr):
                with self.assertRaises(RuntimeError):
                    mod.collector_snapshot()

    def test_processor_scope_and_association(self):
        self.assertEqual(mod.PROCESSOR['filter']['namespace'], 'full-multi')
        self.assertEqual(mod.PROCESSOR['extract']['metadata'], [
            'k8s.namespace.name', 'k8s.pod.name', 'k8s.pod.uid'
        ])
        self.assertEqual(mod.PROCESSOR['pod_association'][1]['sources'][0]['from'], 'connection')


if __name__ == '__main__':
    unittest.main()
