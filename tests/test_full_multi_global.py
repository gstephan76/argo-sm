"""Offline contract tests: no cluster or GitHub access required."""
import importlib.util
from contextlib import contextmanager, ExitStack
import subprocess
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch

P = Path(__file__).resolve().parents[1] / "scripts/full-multi-global.py"
S = importlib.util.spec_from_file_location("full_multi_global", P)
M = importlib.util.module_from_spec(S)
S.loader.exec_module(M)


class GlobalWorkflowTests(unittest.TestCase):
    @contextmanager
    def installation(self, failure=None):
        with ExitStack() as stack:
            mocks = {name: stack.enter_context(patch.object(M, name)) for name in (
                'assert_git_clean', 'preflight_local', 'verify_images', 'check_rollout_manager',
                'assert_no_legacy_mariadb', 'verify_db_volumes', 'commit_blue', 'ensure_gitops_app',
                'wait_gitops', 'git', 'rebaseline', 'maybe_oc_json', 'call')}
            stack.enter_context(patch.object(M.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')))
            mocks['git'].return_value = 'testrevision'
            mocks['maybe_oc_json'].return_value = None

            def command(args, **kwargs):
                if failure and failure(args):
                    raise M.Failure('simulated interrupted stage')
                return 'rendered manifests'

            mocks['call'].side_effect = command
            yield mocks

    def test_unscoped_prepare_routes_to_complete_installation(self):
        with patch.object(M, 'setup') as setup, patch.object(M, 'rebaseline') as reset:
            self.assertEqual(M.main(['prepare', '--apply', '--push']), 0)
        setup.assert_called_once()
        self.assertEqual(setup.call_args.args[0].instance, 'all')
        reset.assert_not_called()

    def test_complete_prepare_runs_prerequisites_in_order_before_databases(self):
        args = SimpleNamespace(instance='all', timeout=600)
        with self.installation() as mocks:
            M.setup(args)
        mocks['preflight_local'].assert_called_once_with(require_prereqs=False)
        commands = [call.args[0] for call in mocks['call'].call_args_list]
        self.assertEqual([command[1:] for command in commands[:5]], [
            ['scripts/full-multi-ambient-network.py', '--check'],
            ['scripts/full-multi-ambient-network.py', '--apply'],
            ['scripts/full-multi-install-prereqs.py', '--check'],
            ['scripts/full-multi-install-prereqs.py', '--apply'],
            ['scripts/full-multi-secrets.sh', '--generate-demo']])
        self.assertIn(['bash', 'scripts/full-multi-databases.sh', '--apply'], commands[5:])
        mocks['commit_blue'].assert_called_once_with()
        mocks['ensure_gitops_app'].assert_called_once_with()
        mocks['rebaseline'].assert_called_once_with(args, verified=True)

    def test_complete_prepare_stops_after_each_failed_prerequisite(self):
        stages = [('full-multi-ambient-network.py', '--check'),
                  ('full-multi-ambient-network.py', '--apply'),
                  ('full-multi-install-prereqs.py', '--check'),
                  ('full-multi-install-prereqs.py', '--apply'),
                  ('full-multi-secrets.sh', '--generate-demo')]
        for index, (script, flag) in enumerate(stages):
            with self.subTest(stage=(script, flag)), \
                 self.installation(lambda args: args[1:] == [f'scripts/{script}', flag]) as mocks:
                self.assertEqual(M.main(['prepare', '--apply', '--push']), 1)
            self.assertEqual(mocks['call'].call_count, index + 1)
            mocks['commit_blue'].assert_not_called()
            mocks['ensure_gitops_app'].assert_not_called()
            mocks['rebaseline'].assert_not_called()

    def test_complete_prepare_rerun_reuses_existing_application(self):
        args = SimpleNamespace(instance='all', timeout=600)
        with self.installation() as mocks:
            M.setup(args)
            mocks['maybe_oc_json'].return_value = {'metadata': {'name': 'multi-ambient'}}
            M.setup(args)
        # Only the first installation needs Blue published before Application creation.
        mocks['commit_blue'].assert_called_once_with()
        self.assertEqual(mocks['ensure_gitops_app'].call_count, 2)
        self.assertEqual(mocks['rebaseline'].call_count, 2)

    def test_fresh_local_preflight_does_not_require_installed_gitops(self):
        with patch.object(M.shutil, 'which', return_value='/bin/tool'), patch.object(M, 'call'), \
             patch.object(M, 'ocjson') as read, patch.object(M, 'maybe_oc_json') as optional:
            M.preflight_local(require_prereqs=False)
        read.assert_not_called()
        optional.assert_not_called()

    def test_available_manager_still_waits_for_actual_controller(self):
        managers = {'items': [{'metadata': {'namespace': 'openshift-gitops'},
                               'spec': {'namespaceScoped': False},
                               'status': {'phase': 'Available', 'rolloutController': 'Available'}}]}
        with patch.object(M, 'ocjson', return_value=managers), \
             patch.object(M, 'maybe_oc_json', side_effect=[None, {'status': {'availableReplicas': 1}}]) as read, \
             patch.object(M.time, 'sleep') as sleep, patch.object(M, 'call') as command:
            M.check_rollout_manager()
        self.assertEqual(read.call_count, 2)
        sleep.assert_called_once()
        command.assert_not_called()

    def test_blue_templates_are_specific_to_application(self):
        def obj(role, app, track="blue", marker="baseline-blue"):
            env = [{"name": "MESSAGE_PREFIX", "value": f"Application {app.upper()} - BLUE"},
                   {"name": "DB_TRACK", "value": track},
                   {"name": "DB_HOST", "value": f"mariadb-{app}-{track}.multi-ambient.svc.cluster.local"}]
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

    def test_pending_global_commit_is_verified_before_exact_push(self):
        origin='https://github.com/gstephan76/argo-sm.git'
        local='a'*40
        remote='b'*40
        responses=['multi-ambient', origin, origin, '', local, remote, local, local, local]
        pending=subprocess.CompletedProcess([], 0, local+'\n', '')
        with patch.object(M, 'git', side_effect=responses), patch.object(M, 'silent', return_value=pending), patch.object(M, 'call') as call:
            self.assertEqual(M.assert_git_clean(), local)
        commands=[c.args[0] for c in call.call_args_list]
        verify=next(i for i,c in enumerate(commands) if 'verify-pending' in c)
        push=next(i for i,c in enumerate(commands) if c[:2]==['git','push'])
        self.assertLess(verify,push)
        self.assertEqual(commands[push], ['git','push','origin',f'{local}:refs/heads/multi-ambient'])

    def test_invalid_pending_global_commit_is_never_pushed(self):
        origin='https://github.com/gstephan76/argo-sm.git'
        responses=['multi-ambient', origin, origin, '', 'a'*40, 'b'*40]
        def execute(args, **kwargs):
            if 'verify-pending' in args:
                raise M.Failure('invalid pending commit')
            return ''
        pending=subprocess.CompletedProcess([], 0, 'a'*40+'\n', '')
        with patch.object(M, 'git', side_effect=responses), patch.object(M, 'silent', return_value=pending), patch.object(M, 'call', side_effect=execute) as call:
            with self.assertRaisesRegex(M.Failure,'invalid pending commit'):
                M.assert_git_clean()
        self.assertFalse(any(c.args[0][:2]==['git','push'] for c in call.call_args_list))

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

    def test_rebaseline_retries_only_verified_blue_aborted_revision(self):
        seen = set()
        def fake_rollout(name):
            aborted = name == 'crud-a' and name not in seen
            seen.add(name)
            return {'metadata': {'generation': 5}, 'spec': {'replicas': 1},
                    'status': {'phase': 'Degraded' if aborted else 'Healthy',
                               'abort': aborted, 'observedGeneration': '5',
                               'availableReplicas': '1', 'stableRS': 'stable',
                               'currentPodHash': 'stable'}}
        with patch.object(M, 'rollout', side_effect=fake_rollout), \
             patch.object(M, 'blue_template', return_value=True), \
             patch.object(M, 'call') as commands, patch.object(M.time, 'sleep'):
            M.wait_blue_rollouts(timeout=5)
            commands.assert_called_once_with(
                ['oc', 'argo', 'rollouts', 'retry', 'rollout', 'crud-a', '-n', 'multi-ambient'], timeout=60)

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
