"""Candidate generation must preserve a verified existing revision on retry."""
import contextlib
import importlib.util
import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('full_multi_spec', ROOT / 'scripts/full-multi-spec.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class CandidateRetryTests(unittest.TestCase):
    def test_existing_candidate_is_unchanged_on_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'full-multi'
            shutil.copytree(ROOT / 'full-multi', target)
            with patch.object(module, 'D', target), contextlib.redirect_stdout(io.StringIO()):
                module.setcolor('blue', 'a')
                module.setcolor('yellow', 'a')
                paths = [target / f'rollout-{tier}-a.yaml' for tier in module.TIERS]
                before = [p.read_bytes() for p in paths]
                module.setcolor('yellow', 'a')
                self.assertEqual(before, [p.read_bytes() for p in paths])

    def test_inconsistent_existing_candidate_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'full-multi'
            shutil.copytree(ROOT / 'full-multi', target)
            with patch.object(module, 'D', target), contextlib.redirect_stdout(io.StringIO()):
                module.setcolor('blue', 'a')
                module.setcolor('yellow', 'a')
                path = target / 'rollout-front-a.yaml'
                path.write_text(path.read_text().replace('candidate-yellow-', 'unknown-yellow-'))
                with self.assertRaisesRegex(AssertionError, 'unrecognized candidate'):
                    module.setcolor('yellow', 'a')

    def test_pending_commit_requires_exact_workflow_transformation(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            target = repo / 'full-multi'
            shutil.copytree(ROOT / 'full-multi', target)
            def git(*args):
                return subprocess.run(['git', *args], cwd=repo, check=True, text=True,
                                      capture_output=True).stdout.strip()
            git('init', '-q', '-b', 'main')
            git('config', 'user.name', 'Test')
            git('config', 'user.email', 'test@example.invalid')
            with patch.object(module, 'D', target), patch.object(module, 'ROOT', repo), \
                 contextlib.redirect_stdout(io.StringIO()):
                module.setcolor('blue', 'a')
                git('add', 'full-multi')
                git('commit', '-qm', 'baseline')
                base = git('rev-parse', 'HEAD')
                module.setcolor('yellow', 'a')
                git('add', 'full-multi')
                git('commit', '-qm', 'Set full-multi instance a yellow')
                head = git('rev-parse', 'HEAD')
                with self.assertRaises(subprocess.CalledProcessError):
                    module.verify_pending('a', 'yellow', base, head)
                git('update-ref', 'refs/full-multi/pending', head, '')
                module.verify_pending('a', 'yellow', base, head)
                path = target / 'rollout-front-a.yaml'
                path.write_text(path.read_text().replace('periodSeconds: 5', 'periodSeconds: 6', 1))
                git('add', 'full-multi')
                git('commit', '--amend', '--no-edit', '-q')
                git('update-ref', 'refs/full-multi/pending', git('rev-parse', 'HEAD'), head)
                with self.assertRaisesRegex(AssertionError, 'Unexpected change'):
                    module.verify_pending('a', 'yellow', base, git('rev-parse', 'HEAD'))
                previous=git('rev-parse', 'HEAD')
                path.write_text(path.read_text().replace('periodSeconds: 6', 'periodSeconds: 5', 1))
                other=target / 'namespace.yaml'
                other.write_text(other.read_text()+'# unrelated change\n')
                git('add', 'full-multi')
                git('commit', '--amend', '--no-edit', '-q')
                git('update-ref', 'refs/full-multi/pending', git('rev-parse', 'HEAD'), previous)
                with self.assertRaisesRegex(AssertionError, 'unexpected paths'):
                    module.verify_pending('a', 'yellow', base, git('rev-parse', 'HEAD'))


if __name__ == '__main__':
    unittest.main()
