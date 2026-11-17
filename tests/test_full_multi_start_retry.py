"""Exercise retry orchestration with simulated Git, Argo CD and Rollouts."""
import os
import re
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
GIT = '''#!/usr/bin/python3
import os, sys
args = sys.argv[1:]
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write('git ' + ' '.join(args) + '\\n')
if args[:2] == ['branch', '--show-current']:
    print('multi-ambient')
elif args and args[0] == 'rev-parse':
    if 'refs/full-multi/pending' in args:
        sys.exit(1)
    print('abc123')
elif args[:2] == ['remote', 'get-url']:
    print('https://github.com/gstephan76/argo-sm.git')
'''
OC = '''#!/usr/bin/python3
import os, pathlib, re, sys
args = sys.argv[1:]
if 'get' in args and 'rollout' in args:
    name = args[args.index('rollout') + 1]
    fmt = next((x for x in args if x.startswith('jsonpath=')), '')
    if 'demo-rollout-revision' in fmt:
        text = pathlib.Path('full-multi/rollout-' + name + '.yaml').read_text()
        marker = re.search(r'demo-rollout-revision: (\\S+)', text).group(1)
        print('unexpected' if name == os.environ.get('BAD_ROLLOUT') else marker, end='')
    elif 'observedGeneration' in fmt or 'metadata.generation' in fmt:
        print('2', end='')
    elif 'currentStepIndex' in fmt:
        print('6', end='')
    elif 'status.phase' in fmt:
        print('Paused', end='')
'''
PYTHON = '''#!/usr/bin/python3
import os, sys
with open(os.environ['CALL_LOG'], 'a') as f:
    f.write('python ' + ' '.join(sys.argv[1:]) + '\\n')
'''


class StartRetryTests(unittest.TestCase):
    def run_start(self, bad_rollout=''):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / 'full-multi', root / 'full-multi')
            for path in (root / 'full-multi').glob('rollout-*.yaml'):
                text = path.read_text()
                path.write_text(re.sub(r'(?m)^(        demo-rollout-revision: )\S+',
                                       r'\g<1>candidate-yellow-20261115T233842Z', text))
            (root / 'scripts').mkdir()
            for name in ('full-multi.sh', 'full-multi-db-readonly.sh'):
                shutil.copy(ROOT / 'scripts' / name, root / 'scripts' / name)
            (root / 'scripts/full-multi-validate-data-paths.sh').write_text('#!/bin/bash\nexit 0\n')
            bindir = root / 'bin'
            bindir.mkdir()
            for name, source in (('git', GIT), ('oc', OC), ('python3', PYTHON)):
                path = bindir / name
                path.write_text(source)
                path.chmod(0o755)
            log = root / 'calls'
            env = dict(os.environ, PATH=f'{bindir}:{os.environ["PATH"]}',
                       CALL_LOG=str(log), BAD_ROLLOUT=bad_rollout)
            env.pop('INSTANCE', None)
            result = subprocess.run(['/bin/bash', str(root / 'scripts/full-multi.sh'), 'start'],
                                    cwd=root, env=env, capture_output=True, text=True, timeout=10)
            return result, log.read_text()

    def test_both_paused_candidates_resume_without_git_write(self):
        result, calls = self.run_start()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Resuming existing candidate', result.stdout)
        self.assertNotIn('git commit', calls)
        self.assertNotIn('git push', calls)
        self.assertIn('full-multi-crud-pause-gate.py --instance all', calls)

    def test_mismatched_live_candidate_fails(self):
        result, _ = self.run_start('backend-b')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('live candidate unexpected differs from Git', result.stderr)


if __name__ == '__main__':
    unittest.main()
