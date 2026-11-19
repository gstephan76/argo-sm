"""Regression tests for scoped prepare; mocked Git/oc, no cluster mutations."""
import importlib.util
import os
import subprocess
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

P = Path(__file__).resolve().parents[1] / "scripts/full-multi-global.py"
S = importlib.util.spec_from_file_location("full_multi_global_scoped", P)
M = importlib.util.module_from_spec(S)
S.loader.exec_module(M)


class ScopedPrepareTests(unittest.TestCase):
    def test_canonical_prepare_defaults_to_both_and_preserves_explicit_scope(self):
        root = P.parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            python = Path(tmp) / 'python3'
            python.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\n')
            python.chmod(0o755)
            for instance in (None, 'all', 'a', 'b'):
                env = dict(os.environ, PATH=f'{tmp}:{os.environ["PATH"]}')
                env.pop('INSTANCE', None)
                if instance is not None:
                    env['INSTANCE'] = instance
                with self.subTest(instance=instance):
                    result = subprocess.run(['bash', str(root / 'scripts/full-multi'), 'prepare'],
                                            env=env, capture_output=True, text=True, timeout=10)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.splitlines(), [
                        'scripts/full-multi-global.py', 'prepare', '--instance', instance or 'all', '--apply', '--push'])

    def test_scoped_prepare_executes_only_a(self):
        args = SimpleNamespace(instance="a", timeout=600)
        with patch.object(M, "assert_git_clean") as clean, \
             patch.object(M, "preflight_local"), \
             patch.object(M, "call") as call, \
             patch.object(M, "verify_db_volumes"), \
             patch.object(M, "abort_pending_rollouts") as abort, \
             patch.object(M, "commit_blue", return_value="testrevision") as commit, \
             patch.object(M, "wait_gitops") as sync, \
             patch.object(M, "wait_blue_rollouts") as ready, \
             patch.object(M, "baseline_e2e") as e2e:
            M.rebaseline(args)
        clean.assert_called_once_with(instance="a")
        abort.assert_called_once_with(("a",))
        commit.assert_called_once_with(("a",))
        sync.assert_called_once_with("testrevision")
        ready.assert_called_once_with(timeout=600, apps=("a",))
        self.assertEqual(e2e.call_count, 2)
        self.assertTrue(all(c.args[0] == ("a",) for c in e2e.call_args_list))
        commands = [c.args[0] for c in call.call_args_list]
        self.assertFalse(any("--apply" in cmd for cmd in commands))
        hist = [cmd for cmd in commands if "scripts/full-multi-history-reset.py" in cmd]
        self.assertEqual(len(hist), 1)
        self.assertEqual(hist[0][hist[0].index("--instance") + 1], "a")

    def test_scoped_abort_never_reads_b(self):
        def fake_rollout(name):
            if name.endswith("-b"):
                raise AssertionError("Unexpected B query")
            return {"status": {"phase": "Paused", "stableRS": "old", "currentPodHash": "new"}}
        with patch.object(M, "rollout", side_effect=fake_rollout), patch.object(M, "call") as call:
            M.abort_pending_rollouts(("a",))
        self.assertEqual(call.call_count, 3)
        for cmd in (c.args[0] for c in call.call_args_list):
            self.assertTrue(cmd[4].endswith("-a"), cmd)

    def test_scoped_wait_never_reads_b(self):
        def fake_rollout(name):
            if name.endswith("-b"):
                raise AssertionError("Unexpected B query")
            return {"metadata": {"generation": 2}, "spec": {"replicas": 1},
                    "status": {"phase": "Healthy", "observedGeneration": 2,
                               "stableRS": "hash", "currentPodHash": "hash",
                               "availableReplicas": 1}}
        with patch.object(M, "rollout", side_effect=fake_rollout), \
             patch.object(M, "blue_template", return_value=True), \
             patch.object(M, "call") as call:
            M.wait_blue_rollouts(timeout=5, apps=("a",))
        call.assert_not_called()

    def test_commit_blue_changes_only_selected_app(self):
        def fake_git(*args, **kwargs):
            if args[:3] == ("diff", "--cached", "--name-only"):
                return "full-multi/rollout-crud-a.yaml\n"
            if args[:2] == ("rev-parse", "HEAD"):
                return "a" * 40
            raise AssertionError(args)
        with patch.object(M, "git", side_effect=fake_git), patch.object(M, "call") as call:
            self.assertEqual(M.commit_blue(("a",)), "a" * 40)
        cmds = [c.args[0] for c in call.call_args_list]
        setters = [c for c in cmds if "scripts/full-multi-spec.py" in c]
        self.assertEqual(len(setters), 1)
        self.assertEqual(setters[0][setters[0].index("--instance") + 1], "a")
        add = next(cmd for cmd in cmds if cmd[:2] == ["git", "add"])
        self.assertEqual(len([p for p in add if "rollout-" in p]), 3)
        self.assertTrue(all("-a.yaml" in p for p in add if "rollout-" in p))
        commit = next(cmd for cmd in cmds if cmd[:2] == ["git", "commit"])
        self.assertEqual(commit[-1], "Set full-multi instance a blue")

    def test_selected_main_requires_authorization(self):
        with patch.object(M, "rebaseline") as r:
            self.assertEqual(M.main(["prepare", "--instance", "a"]), 0)
            r.assert_not_called()
            self.assertEqual(M.main(["prepare", "--instance", "a", "--apply", "--push"]), 0)
            r.assert_called_once()

    def test_shell_delegates_prepare_to_global(self):
        shell = (Path(__file__).resolve().parents[1] / "scripts/full-multi.sh").read_text()
        self.assertIn('exec python3 scripts/full-multi-global.py prepare --instance "$INSTANCE" --apply --push', shell)
        self.assertNotIn('prepare|start)', shell)
        wrapper = (Path(__file__).resolve().parents[1] / "scripts/full-multi").read_text()
        self.assertIn('full-multi.sh', wrapper)


if __name__ == "__main__":
    unittest.main()
