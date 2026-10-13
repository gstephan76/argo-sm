#!/usr/bin/env python3
"""Safely remove historical ReplicaSets and AnalysisRuns after BLUE prepare.

The live BLUE ReplicaSet and other application instances are never deleted.
Only resources with an exact Rollout UID owner reference qualify for deletion.
"""
import argparse
import json
import subprocess
import sys
import time

NAMESPACE = "full-single-service"
POLL_SECONDS = 5
TERMINAL_ANALYSIS = {"Successful", "Failed", "Inconclusive", "Error"}


class ResetError(RuntimeError):
    pass


def oc(*args, output=False):
    command = ["oc", "--request-timeout=12s", *args]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=True, timeout=35)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = getattr(exc, "stderr", None) or str(exc)
        raise ResetError(f"oc command failed: {' '.join(command)}: {detail}") from exc
    if not output and result.stdout.strip():
        print(result.stdout.strip(), flush=True)
    return result.stdout


def get_json(kind, name=None):
    args = ["get", kind]
    if name is not None:
        args.append(name)
    args += ["-n", NAMESPACE, "-o", "json"]
    return json.loads(oc(*args, output=True))


def owned_by(resource, rollout, uid):
    return any(
        r.get("kind") == "Rollout" and r.get("name") == rollout
        and r.get("uid") == uid
        for r in (resource.get("metadata", {}).get("ownerReferences") or [])
    )


def validate_blue(rollout_obj, instance, expected_uid=None, expected_stable=None):
    name = f"full-single-service-{instance}"
    metadata = rollout_obj.get("metadata") or {}
    spec = rollout_obj.get("spec") or {}
    status = rollout_obj.get("status") or {}
    uid = metadata.get("uid")
    stable = status.get("stableRS")
    containers = ((spec.get("template") or {}).get("spec") or {}).get("containers") or []
    env = containers[0].get("env") or [] if containers else []
    prefix = next((i.get("value") for i in env if i.get("name") == "MESSAGE_PREFIX"), None)
    marker = ((spec.get("template") or {}).get("metadata") or {}).get("annotations", {}).get("demo-rollout-revision")
    good = (
        metadata.get("name") == name and bool(uid) and bool(stable)
        and status.get("phase") == "Healthy"
        and status.get("currentPodHash") == stable
        and spec.get("replicas") == 1
        and status.get("updatedReplicas") == 1
        and status.get("availableReplicas") == 1
        and prefix == f"Application {instance.upper()} - BLUE"
        and marker == "baseline-blue"
        and (expected_uid is None or uid == expected_uid)
        and (expected_stable is None or stable == expected_stable)
    )
    if not good:
        details = {
            "uid": uid,
            "name": metadata.get("name"),
            "phase": status.get("phase"),
            "desired": spec.get("replicas"),
            "updated": status.get("updatedReplicas"),
            "available": status.get("availableReplicas"),
            "stableRS": stable,
            "currentPodHash": status.get("currentPodHash"),
            "prefix": prefix,
            "marker": marker,
            "expected_uid": expected_uid,
            "expected_stable": expected_stable,
        }
        raise ResetError(
            f"Refusing history reset until {name} is a stable, fully available "
            f"BLUE baseline: {json.dumps(details, sort_keys=True)}"
        )
    return uid, stable


def wait_for_blue(instance, deadline, expected_uid=None, expected_stable=None):
    """Wait for status propagation without relaxing the BLUE deletion guards."""
    rollout = f"full-single-service-{instance}"
    while True:
        obj = get_json("rollouts.argoproj.io", rollout)
        metadata = obj.get("metadata") or {}
        status = obj.get("status") or {}
        spec = obj.get("spec") or {}
        template = spec.get("template") or {}
        env = ((template.get("spec") or {}).get("containers") or [{}])[0].get("env") or []
        prefix = next((e.get("value") for e in env if e.get("name") == "MESSAGE_PREFIX"), None)
        marker = ((template.get("metadata") or {}).get("annotations") or {}).get("demo-rollout-revision")
        # A replacement Rollout, a different stable hash, or non-BLUE desired
        # state must never cause an old cleanup operation to resume later.
        if expected_uid is not None and metadata.get("uid") != expected_uid:
            raise ResetError("Rollout UID changed while waiting; refusing history reset")
        if expected_stable is not None and status.get("stableRS") != expected_stable:
            raise ResetError("Stable ReplicaSet changed while waiting; refusing history reset")
        if prefix != f"Application {instance.upper()} - BLUE" or marker != "baseline-blue":
            raise ResetError("Expected BLUE baseline changed while waiting; refusing history reset")
        if status.get("phase") == "Degraded":
            raise ResetError("BLUE rollout is Degraded; refusing history reset")
        try:
            return validate_blue(obj, instance, expected_uid, expected_stable)
        except ResetError as exc:
            if time.monotonic() >= deadline:
                raise ResetError(f"Timed out waiting for fully available BLUE state: {exc}") from exc
            print(f"Waiting for BLUE readiness before history cleanup: {exc}", flush=True)
            time.sleep(POLL_SECONDS)


def inspect_resources(rs_list, ar_list, instance, uid, stable):
    rollout = f"full-single-service-{instance}"
    stable_name = f"{rollout}-{stable}"
    replica_sets = [rs for rs in rs_list.get("items", []) if owned_by(rs, rollout, uid)]
    analysis_runs = [ar for ar in ar_list.get("items", []) if owned_by(ar, rollout, uid)]
    stable_matches = [rs for rs in replica_sets if rs.get("metadata", {}).get("name") == stable_name]
    if len(stable_matches) != 1:
        raise ResetError(f"Expected precisely one owned BLUE ReplicaSet {stable_name}")
    stable_rs = stable_matches[0]
    if stable_rs.get("spec", {}).get("replicas") != 1 or stable_rs.get("status", {}).get("readyReplicas") != 1:
        raise ResetError("BLUE ReplicaSet is not fully ready; refusing cleanup")

    old_rs = [rs for rs in replica_sets if rs is not stable_rs]
    live_old = [rs.get("metadata", {}).get("name") for rs in old_rs if
                (rs.get("spec", {}).get("replicas") or 0) != 0 or
                (rs.get("status", {}).get("replicas") or 0) != 0]
    active_ar = [ar.get("metadata", {}).get("name") for ar in analysis_runs if
                 ar.get("status", {}).get("phase") not in TERMINAL_ANALYSIS]
    return [rs["metadata"]["name"] for rs in old_rs], [ar["metadata"]["name"] for ar in analysis_runs], live_old, active_ar


def run(instance, timeout_seconds, dry_run=False):
    rollout = f"full-single-service-{instance}"
    deadline = time.monotonic() + timeout_seconds
    uid, stable = wait_for_blue(instance, deadline)
    while True:
        wait_for_blue(instance, deadline, uid, stable)
        old_rs, runs, live_old, active_ar = inspect_resources(
            get_json("replicasets.apps"), get_json("analysisruns.argoproj.io"),
            instance, uid, stable,
        )
        if not (live_old or active_ar):
            break
        if time.monotonic() >= deadline:
            raise ResetError(f"Timed out waiting for safe cleanup: old live ReplicaSets={live_old}; active analyses={active_ar}")
        print(f"Waiting for old ReplicaSets to scale down and analysis to finish: RS={live_old} AR={active_ar}", flush=True)
        time.sleep(POLL_SECONDS)

    print(f"BLUE baseline protected: {rollout}-{stable}")
    print(f"History reset: {len(runs)} completed AnalysisRuns, {len(old_rs)} old ReplicaSets")
    if dry_run:
        print("DRY RUN: no deletions performed")
        print("AnalysisRuns:", ", ".join(runs) or "none")
        print("Old ReplicaSets:", ", ".join(old_rs) or "none")
        return

    # All destructive commands are strictly by exact object name and namespace.
    # Check the baseline before every delete in case another operator changed it.
    for kind, names in (("analysisruns.argoproj.io", runs), ("replicasets.apps", old_rs)):
        for name in names:
            validate_blue(get_json("rollouts.argoproj.io", rollout), instance, uid, stable)
            obj = None
            try:
                obj = get_json(kind, name)
            except ResetError as exc:
                # Do not ignore API errors here, including RBAC or network failures.
                raise ResetError(f"Cannot verify resource {kind}/{name} before deleting it: {exc}") from exc
            if not owned_by(obj, rollout, uid):
                raise ResetError(f"Owner changed on {kind}/{name}; refusing delete")
            if kind == "replicasets.apps":
                if (obj.get("spec", {}).get("replicas") or 0) != 0 or (obj.get("status", {}).get("replicas") or 0) != 0:
                    raise ResetError(f"Old ReplicaSet {name} has live replicas; refusing delete")
                if name == f"{rollout}-{stable}":
                    raise ResetError("Attempt to delete active BLUE ReplicaSet blocked")
            elif obj.get("status", {}).get("phase") not in TERMINAL_ANALYSIS:
                raise ResetError(f"AnalysisRun {name} is active; refusing delete")
            print(f"Deleting historical {kind}/{name}", flush=True)
            oc("delete", kind, name, "-n", NAMESPACE, "--wait=true", "--timeout=30s")

    validate_blue(get_json("rollouts.argoproj.io", rollout), instance, uid, stable)
    old_rs, runs, live_old, active_ar = inspect_resources(
        get_json("replicasets.apps"), get_json("analysisruns.argoproj.io"), instance, uid, stable,
    )
    if old_rs or runs:
        raise ResetError(f"History cleanup incomplete: ReplicaSets={old_rs}, AnalysisRuns={runs}")
    print(f"PASS: {rollout} BLUE healthy; history cleared; active ReplicaSet preserved")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--instance", choices=("a", "b"), required=True)
    parser.add_argument("--timeout-seconds", type=int, default=120)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    if args.timeout_seconds < 1:
        parser.error("--timeout-seconds must be positive")
    try:
        run(args.instance, args.timeout_seconds, args.dry_run)
    except (ResetError, ValueError, KeyError) as exc:
        print(f"ERROR: History reset did not complete: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
