"""Offline regression tests for the CRUD BlueGreen paused-state gate."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

MODULE=Path(__file__).resolve().parents[1]/'scripts'/'full-multi-crud-pause-gate.py'
spec=importlib.util.spec_from_file_location('crud_pause_gate',MODULE)
gate=importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)

def sample(track, h, app='a'):
    return {
        'metadata': {'name':f'crud-{app}-{track}-pod',
                     'labels':{'app':f'crud-{app}','demo.crud-track':track,
                               'rollouts-pod-template-hash':h}},
        'spec':{'containers':[{'name':'crud','env':[
            {'name':'APP_ID','value':app.upper()},
            {'name':'DB_HOST','value':f'mariadb-{app}-{track}.full-multi.svc.cluster.local'},
            {'name':'DB_TRACK','value':track}]}]},
        'status':{'phase':'Running','conditions':[{'type':'Ready','status':'True'}]}}

def fake_oc(*args):
    kind=args[0]
    if kind=='rollout':
        return {'spec':{'strategy':{'blueGreen':{'autoPromotionEnabled':False,
               'activeService':'crud-a-active','previewService':'crud-a-preview',
               'previewReplicaCount':1}}},
               'status':{'phase':'Paused','stableRS':'stablehash','currentPodHash':'previewhash'}}
    if kind=='pods': return {'items':[sample('blue','stablehash'),sample('green','previewhash')]}
    if kind=='statefulset':return {'status':{'readyReplicas':1}}
    if kind=='pvc':
        track='blue' if '-blue-' in args[1] else 'green'
        return {'spec':{'volumeName':f'pv-a-{track}'},'status':{'phase':'Bound'}}
    if kind=='endpointslices.discovery.k8s.io':
        svc=args[-1].split('=',1)[1]
        track='blue' if svc.endswith('-blue') or svc.endswith('-active') else 'green'
        return {'items':[{'endpoints':[{'conditions':{'ready':True},
                'targetRef':{'kind':'Pod','name':f'crud-a-{track}-pod'}}]}]}
    raise AssertionError(args)

class PauseGateTest(unittest.TestCase):
    def test_valid_paused_blue_and_green(self):
        with patch.object(gate,'oc',side_effect=fake_oc):
            self.assertEqual(gate.verify('a'),['pv-a-blue','pv-a-green'])

    def test_empty_green_endpoint_fails(self):
        def bad(*args):
            result=fake_oc(*args)
            if args[0]=='endpointslices.discovery.k8s.io' and args[-1].endswith('crud-a-green'):
                return {'items':[]}
            return result
        with patch.object(gate,'oc',side_effect=bad):
            with self.assertRaises(gate.Failed):gate.verify('a')

    def test_shared_pv_fails(self):
        def bad(*args):
            result=fake_oc(*args)
            if args[0]=='pvc':result['spec']['volumeName']='same-pv'
            return result
        with patch.object(gate,'oc',side_effect=bad):
            with self.assertRaises(gate.Failed):gate.verify('a')

    def test_auto_promotion_fails(self):
        def bad(*args):
            result=fake_oc(*args)
            if args[0]=='rollout':result['spec']['strategy']['blueGreen']['autoPromotionEnabled']=True
            return result
        with patch.object(gate,'oc',side_effect=bad):
            with self.assertRaises(gate.Failed):gate.verify('a')

if __name__=='__main__':unittest.main()
