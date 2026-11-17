"""Secret provisioning fails closed on API errors and creation races."""
import os
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
    if mode == 'existing':
        print('secret/' + args[args.index('secret') + 1])
if 'create' in args:
    content = sys.stdin.read()
    if os.environ['OC_TEST_MODE'] == 'demo':
        docs = [json.loads(x) for x in content.split('---') if x.strip()]
        assert len(docs) == 2
        assert all(x['metadata']['namespace'] == 'multi-ambient' for x in docs)
        p = [base64.b64decode(x['data']['password']) for x in docs]
        r = [base64.b64decode(x['data']['root-password']) for x in docs]
        assert len(p[0]) >= 32 and len(r[0]) >= 32 and p[0] == p[1] and r[0] == r[1] and p[0] != r[0]
        sys.exit(0)
    print('AlreadyExists', file=sys.stderr)
    sys.exit(1)
'''


class SecretProvisioningTests(unittest.TestCase):
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

    def test_existing_secrets_are_not_written(self):
        result, calls = self.run_script('existing')
        self.assertEqual(result.returncode, 0)
        self.assertNotIn('create -f -', calls)

    def test_concurrent_creation_fails_without_apply(self):
        result, calls = self.run_script('absent')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('create -f -', calls)
        self.assertNotIn('apply -f -', calls)


if __name__ == '__main__':
    unittest.main()
