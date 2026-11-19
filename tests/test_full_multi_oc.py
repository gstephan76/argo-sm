"""Retry only read operations whose failure is transient."""
import importlib.util
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('multi_oc_test', Path(__file__).resolve().parents[1] / 'scripts/full_multi_oc.py')
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


class OcRetryTests(unittest.TestCase):
    def test_transient_get_retries_both_namespace_forms(self):
        for args in [('get', 'pods'), ('-n', 'demo', 'get', 'pods'),
                     ('--namespace=demo', 'get', 'pods')]:
            failed = subprocess.CompletedProcess([], 1, '', 'Unable to connect: connection reset')
            success = subprocess.CompletedProcess([], 0, 'ready', '')
            with self.subTest(args=args), patch.object(mod.subprocess, 'run', side_effect=[failed, success]) as run, \
                 patch.object(mod.time, 'sleep'):
                self.assertEqual(mod.run_oc(*args), 'ready')
                self.assertEqual(run.call_count, 2)

    def test_forbidden_get_and_mutations_are_not_replayed(self):
        for args, error in [(('get', 'pods'), 'Forbidden'),
                            (('create', '-f', '-'), 'connection reset'),
                            (('delete', 'pod', 'old'), 'timeout'),
                            (('exec', 'pod', '--', 'get'), 'timeout')]:
            failed = subprocess.CompletedProcess([], 1, '', error)
            with self.subTest(args=args), patch.object(mod.subprocess, 'run', return_value=failed) as run:
                with self.assertRaises(subprocess.CalledProcessError):
                    mod.run_oc(*args)
                run.assert_called_once()

    def test_subprocess_timeout_is_retried_and_bounded(self):
        with patch.object(mod.subprocess, 'run', side_effect=subprocess.TimeoutExpired('oc', 45)) as run, \
             patch.object(mod.time, 'sleep'), patch.dict(mod.os.environ, {'FULL_MULTI_OC_READ_ATTEMPTS': '3'}):
            with self.assertRaises(subprocess.TimeoutExpired):
                mod.run_oc('get', 'pods')
        self.assertEqual(run.call_count, 3)

    def test_stdin_is_forwarded_once_for_creation(self):
        with patch.object(mod.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'created', '')) as run:
            self.assertEqual(mod.run_oc('create', '-f', '-', data='manifest'), 'created')
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs['input'], 'manifest')


if __name__ == '__main__':
    unittest.main()
