"""Offline contract tests: no cluster or GitHub access required."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

P = Path(__file__).resolve().parents[1] / "scripts/full-multi-global.py"
S = importlib.util.spec_from_file_location("full_multi_global", P)
M = importlib.util.module_from_spec(S)
S.loader.exec_module(M)


class GlobalWorkflowTests(unittest.TestCase):
    def test_blue_templates_are_specific_to_application(self):
        def obj(role, app, track="blue", marker="baseline-blue"):
            env = [{"name": "MESSAGE_PREFIX", "value": f"Application {app.upper()} - BLUE"},
                   {"name": "DB_TRACK", "value": track},
                   {"name": "DB_HOST", "value": f"mariadb-{app}-{track}.full-multi.svc.cluster.local"}]
            return {"spec": {"template": {"metadata": {"annotations": {"demo-rollout-revision": marker},
                                               "labels": {"demo.crud-track": track}},
                                          "spec": {"containers": [{"env": env}]}}}}
        for app in M.APPS:
            for role in M.ROLES:
                name = f"{role}-{app}"
                self.assertTrue(M.blue_template(obj(role, app), name))
                self.assertFalse(M.blue_template(obj(role, app, track="green"), name) if role == "crud" else
                                 M.blue_template(obj(role, app, marker="candidate-yellow"), name))

    def test_four_distinct_volumes_required(self):
        def mock_json(*parts):
            name = parts[-1]
            return {"status": {"phase": "Bound"}, "spec": {"volumeName": name}}
        with patch.object(M, "ocjson", side_effect=mock_json):
            M.verify_db_volumes()
        with patch.object(M, "ocjson", return_value={"status": {"phase": "Bound"},
                                                   "spec": {"volumeName": "shared"}}):
            with self.assertRaises(M.Failure):
                M.verify_db_volumes()

    def test_prepare_requires_apply_and_push(self):
        with patch.object(M, "call") as c, patch.object(M, "rebaseline") as b:
            self.assertEqual(M.main(["prepare"]), 0)
            c.assert_not_called()
            b.assert_not_called()

    def test_setup_requires_apply_and_push(self):
        with patch.object(M, "call") as c, patch.object(M, "setup") as s:
            self.assertEqual(M.main(["setup", "--apply"]), 0)
            c.assert_not_called()
            s.assert_not_called()

    def test_aborts_only_known_pending_rollouts(self):
        statuses = {f"{role}-{app}": {"status": {"phase": "Healthy", "stableRS": "foo", "currentPodHash": "foo"}}
                    for role in M.ROLES for app in M.APPS}
        statuses["crud-a"] = {"status": {"phase": "Paused", "stableRS": "old", "currentPodHash": "new"}}
        with patch.object(M, "rollout", side_effect=lambda name: statuses[name]), patch.object(M, "call") as c:
            M.abort_pending_rollouts()
            self.assertEqual(c.call_count, 1)
            self.assertEqual(c.call_args.args[0][3:5], ["abort", "crud-a"])

    def test_paused_green_is_never_promoted_by_prepare_without_blue_template(self):
        incorrect = {"spec": {"template": {"metadata": {"annotations": {"demo-rollout-revision": "candidate-yellow"},
                                                   "labels": {"demo.crud-track": "green"}},
                                       "spec": {"containers": [{"env": []}]}}, "replicas": 1},
                     "metadata": {"generation": 1},
                     "status": {"phase": "Paused", "observedGeneration": 1}}
        with patch.object(M, "rollout", return_value=incorrect), patch.object(M, "call") as c:
            with self.assertRaises(M.Failure):
                M.wait_blue_rollouts(timeout=1)
            c.assert_not_called()

    def test_global_start_invokes_inbuilt_gate(self):
        workflow = Path(__file__).resolve().parents[1] / 'scripts/full-multi.sh'
        src = workflow.read_text(encoding='utf-8')
        self.assertIn('full-multi-crud-pause-gate.py', src)
        self.assertIn('--instance all', src)
        self.assertIn('full-multi-validate-data-paths.sh', src)
        self.assertIn('INSTANCE="$app" bash', src)


    def test_string_numeric_rollout_status(self):
        def fake_rollout(name):
            return {
                "metadata": {"generation": 5},
                "spec": {"replicas": 1},
                "status": {
                    "phase": "Healthy",
                    "observedGeneration": "5",
                    "availableReplicas": "1",
                    "stableRS": "stable-hash",
                    "currentPodHash": "stable-hash",
                },
            }

        with patch.object(M, "rollout", side_effect=fake_rollout), \
             patch.object(M, "blue_template", return_value=True), \
             patch.object(M, "call") as commands:
            M.wait_blue_rollouts(timeout=5)
            commands.assert_not_called()

    def test_sm_branch_rejection(self):
        with patch.object(M, "git", side_effect=["main", "https://github.com/gstephan76/SM.git"]):
            with self.assertRaises(M.Failure):
                # Simulate a valid local v3.5 installer path.
                import tempfile
                with tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp)
                    (path / "v3/v3.5/scripts").mkdir(parents=True)
                    (path / "v3/v3.5/scripts/apply.sh").write_text("#!/bin/bash\n")
                    M.check_sm_repo(path)


if __name__ == "__main__":
    unittest.main()
