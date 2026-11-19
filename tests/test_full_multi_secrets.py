"""Secret provisioning fails closed on API errors and creation races."""
import os
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/full-multi-secrets.sh'
FAKE_OC = '''#!/usr/bin/env python3
import base64, json, os, sys
args = sys.argv[1:]
with open(os.environ['OC_CALL_LOG'], 'a') as f:
    f.write(' '.join(args) + '\\n')
if 'get' in args:
    mode = os.environ['OC_TEST_MODE']
    if mode == 'error':
        print('Forbidden', file=sys.stderr)
        sys.exit(1)
    if 'crd' in args:
        if mode == 'crd-error':
            print('Forbidden', file=sys.stderr)
            sys.exit(1)
        sys.exit(0)
    if 'secret' not in args:
        items = []
        if mode == 'partial-storage':
            items = [{'metadata': {'name': 'mariadb-data-mariadb-b-blue-0'}}]
        if mode == 'missing-storage':
            items = [{'metadata': {'name': 'mariadb-data-mariadb-a-blue-0'}}]
        if mode == 'partial-reference':
            items = [{'metadata': {'name': 'custom-job'}, 'spec': {'secretKeyRef': {'name': 'db-credentials-b'}}}]
        print(json.dumps({'items': items}))
    elif mode == 'existing' or (mode.startswith('partial') and args[args.index('secret') + 1] == 'db-credentials-a'):
        print('secret/' + args[args.index('secret') + 1])
if 'create' in args:
    content = sys.stdin.read()
    if os.environ['OC_TEST_MODE'] in ('demo', 'partial-demo'):
        docs = [json.loads(x) for x in content.split('---') if x.strip()]
        assert len(docs) == (1 if os.environ['OC_TEST_MODE'] == 'partial-demo' else 2)
        assert all(x['metadata']['namespace'] == 'multi-ambient' for x in docs)
        p = [base64.b64decode(x['data']['password']) for x in docs]
        r = [base64.b64decode(x['data']['root-password']) for x in docs]
        assert len(p[0]) >= 32 and len(r[0]) >= 32 and len(set(p)) == 1 and len(set(r)) == 1 and p[0] != r[0]
        if os.environ['OC_TEST_MODE'] == 'partial-demo':
            assert docs[0]['metadata']['name'] == 'db-credentials-b'
        sys.exit(0)
    print('AlreadyExists', file=sys.stderr)
    sys.exit(1)
'''

STATEFUL_OC = '''#!/usr/bin/env python3
import json, os, pathlib, sys
args = sys.argv[1:]
statefile = pathlib.Path(os.environ['OC_STATE'])
state = json.loads(statefile.read_text()) if statefile.exists() else {'secrets': {}, 'interrupted': False}
with open(os.environ['OC_CALL_LOG'], 'a') as log:
    log.write(' '.join(args) + '\\n')
if 'get' in args:
    if 'crd' in args:
        sys.exit(0)
    elif 'secret' in args:
        name = args[args.index('secret') + 1]
        if name in state['secrets']:
            print('secret/' + name)
    else:
        print('{"items": []}')
elif 'create' in args:
    docs = [json.loads(x) for x in sys.stdin.read().split('---') if x.strip()]
    for doc in docs:
        name = doc['metadata']['name']
        if name in state['secrets']:
            sys.exit(1)
        if name.endswith('-b') and not state['interrupted']:
            state['interrupted'] = True
            statefile.write_text(json.dumps(state))
            print('connection reset', file=sys.stderr)
            sys.exit(1)
        state['secrets'][name] = doc
        statefile.write_text(json.dumps(state))
'''


class SecretProvisioningTests(unittest.TestCase):
    def test_interrupted_pair_resumes_then_repeated_run_preserves_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oc = root / 'oc'
            oc.write_text(STATEFUL_OC)
            oc.chmod(0o755)
            log, statefile = root / 'calls', root / 'state'
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}',
                       OC_CALL_LOG=str(log), OC_STATE=str(statefile))
            command = ['bash', str(SCRIPT), '--generate-demo']
            first = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
            self.assertNotEqual(first.returncode, 0)
            before = json.loads(statefile.read_text())['secrets']['db-credentials-a']
            second = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(second.returncode, 0, second.stderr)
            after = json.loads(statefile.read_text())['secrets']
            self.assertEqual(before, after['db-credentials-a'])
            self.assertEqual(set(after), {'db-credentials-a', 'db-credentials-b'})
            log.write_text('')
            third = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
            self.assertEqual(third.returncode, 0, third.stderr)
            self.assertEqual(after, json.loads(statefile.read_text())['secrets'])
            self.assertNotIn('create -f -', log.read_text())

    def run_script(self, mode, args=()):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            oc = root / 'oc'
            oc.write_text(FAKE_OC)
            oc.chmod(0o755)
            log = root / 'calls'
            env = dict(os.environ, PATH=f'{root}:{os.environ["PATH"]}', OC_CALL_LOG=str(log),
                       OC_TEST_MODE=mode, FULL_MULTI_DB_PASSWORD='fake',
                       FULL_MULTI_DB_ROOT_PASSWORD='fake-root')
            result = subprocess.run(['bash', str(SCRIPT), *args], env=env, capture_output=True, text=True)
            return result, log.read_text()

    def test_generate_demo_creates_both_secrets_without_printing_values(self):
        result, calls = self.run_script('demo', ('--generate-demo',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('create -f -', calls)
        self.assertNotIn('fake-root', result.stdout + result.stderr + calls)
        self.assertNotIn('fake', result.stdout + result.stderr + calls)

    def test_get_error_does_not_attempt_secret_write(self):
        result, calls = self.run_script('error')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('create -f -', calls)

    def test_crd_check_error_does_not_authorize_credential_generation(self):
        result, calls = self.run_script('crd-error', ('--generate-demo',))
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('create -f -', calls)

    def test_existing_secrets_are_not_written(self):
        result, calls = self.run_script('existing')
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('create -f -', calls)

    def test_concurrent_creation_fails_without_apply(self):
        result, calls = self.run_script('absent')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('create -f -', calls)
        self.assertNotIn('apply -f -', calls)

    def test_partial_demo_creates_only_missing_secret(self):
        result, calls = self.run_script('partial-demo', ('--generate-demo',))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('create -f -', calls)

    def test_missing_secret_with_storage_or_workload_is_not_regenerated(self):
        for mode in ('partial-storage', 'partial-reference', 'missing-storage'):
            with self.subTest(mode=mode):
                result, calls = self.run_script(mode, ('--generate-demo',))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('restore its original credentials', result.stderr)
                self.assertNotIn('create -f -', calls)

    def test_partial_non_demo_still_requires_explicit_recovery(self):
        result, calls = self.run_script('partial-demo')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('create -f -', calls)


if __name__ == '__main__':
    unittest.main()
