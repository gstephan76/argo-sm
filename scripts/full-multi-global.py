#!/usr/bin/env python3
"""Full-multi global setup and deterministic Blue rebaseline.

Canonical external dependency: gstephan76/SM branch v3.5, v3/v3.5/.
All writes require --apply. GitOps changes require --push. No PVC deletion.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
EXPECTED_SM_REMOTE = "github.com/gstephan76/SM"
EXPECTED_APP_REMOTE = "github.com/gstephan76/argo-sm"
ROLES = ("crud", "backend", "front")
APPS = ("a", "b")
TRACKS = ("blue", "green")


class Failure(RuntimeError):
    pass


def log(message: str) -> None:
    print(message, flush=True)


def call(args, *, cwd=None, env=None, timeout=900, capture=False, redact=False):
    args = [str(a) for a in args]
    if not redact:
        log("+ " + " ".join(args))
    else:
        log("+ [credential-bearing command; argv suppressed]")
    try:
        result = subprocess.run(
            args, cwd=cwd, env=env, timeout=timeout,
            text=True, errors="replace", capture_output=capture, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Failure(f"Command failed to start or timed out: {args[0]}: {exc}") from exc
    if result.returncode != 0:
        diagnostics = (result.stderr or result.stdout or "").strip() if capture else "see command output above"
        raise Failure(f"Command failed ({result.returncode}): {args[0]}: {diagnostics[:2000]}")
    return result.stdout.strip() if capture else ""


def silent(args, *, cwd=None, timeout=35):
    return subprocess.run([str(a) for a in args], cwd=cwd, timeout=timeout,
                          text=True, errors="replace", capture_output=True, check=False)


def ocjson(*args, optional=False):
    proc = silent(["oc", *args, "-o", "json"])
    if proc.returncode:
        if optional and ("NotFound" in proc.stderr or "not found" in proc.stderr):
            return None
        raise Failure(f"oc {' '.join(args)}: {proc.stderr.strip()}")
    try:
        return json.loads(proc.stdout)
    except ValueError as exc:
        raise Failure(f"Invalid JSON from oc {' '.join(args)}: {exc}") from exc


def maybe_oc_json(*args):
    proc = silent(["oc", *args, "-o", "json"])
    if proc.returncode:
        if "not found" in proc.stderr.lower() or "notfound" in proc.stderr.lower():
            return None
        raise Failure(f"oc {' '.join(args)}: {proc.stderr.strip()}")
    return json.loads(proc.stdout)


def git(*args, cwd=ROOT):
    return call(["git", *args], cwd=cwd, capture=True, timeout=90)


def assert_git_clean():
    branch = git("branch", "--show-current")
    if branch != "main":
        raise Failure(f"argo-sm must be on branch main; found {branch!r}")
    origin = git("remote", "get-url", "origin")
    if EXPECTED_APP_REMOTE not in origin and "github.com:gstephan76/argo-sm" not in origin:
        raise Failure(f"Unexpected argo-sm origin: {origin}")
    tracked = git("status", "--porcelain", "--untracked-files=no")
    if tracked:
        raise Failure("Tracked changes are present; stash/commit your changes before setup/prepare:\n" + tracked)
    call(["git", "fetch", "origin", "main"], cwd=ROOT, timeout=120)
    local = git("rev-parse", "HEAD")
    remote = git("rev-parse", "origin/main")
    if local != remote:
        raise Failure(f"main and origin/main differ: {local[:12]} / {remote[:12]}; update deliberately")
    return local


def preflight_local():
    for cmd in ("oc", "git", "python3", "bash", "jq", "curl", "skopeo"):
        if not shutil.which(cmd):
            raise Failure(f"Missing local command: {cmd}")
    if not (ROOT / "scripts/full-multi-spec.py").is_file():
        raise Failure(f"Not an argo-sm checkout: {ROOT}")
    call([sys.executable, "scripts/full-multi-spec.py", "verify"], cwd=ROOT, timeout=45)
    for name in ("full-multi.sh", "full-multi-databases.sh", "full-multi-validate-data-paths.sh"):
        call(["bash", "-n", f"scripts/{name}"], cwd=ROOT, timeout=10)
    call(["git", "diff", "--check"], cwd=ROOT, timeout=20)
    call(["oc", "whoami"], timeout=40)
    call(["oc", "get", "namespace", "openshift-gitops"], timeout=35)
    for key in ("applications.argoproj.io", "rollouts.argoproj.io", "rolloutmanagers.argoproj.io"):
        call(["oc", "get", "crd", key], timeout=35)
    app_ns = maybe_oc_json("get", "namespace", "full-multi")
    if app_ns is not None:
        labels = app_ns.get("metadata", {}).get("labels", {})
        if any(labels.get(k) != "enabled" for k in ("istio-discovery", "istio-injection")):
            raise Failure("Existing full-multi namespace lacks required mesh discovery/injection labels")


def check_sm_repo(sm_repo: Path):
    sm_repo = sm_repo.resolve()
    base = sm_repo / "v3/v3.5"
    if not (base / "scripts/apply.sh").exists():
        raise Failure(f"Missing Service Mesh v3.5 installer: {base}/scripts/apply.sh")
    branch = git("branch", "--show-current", cwd=sm_repo)
    origin = git("remote", "get-url", "origin", cwd=sm_repo)
    if branch != "v3.5" or (EXPECTED_SM_REMOTE not in origin and "github.com:gstephan76/SM" not in origin):
        raise Failure(f"SM repo must have origin gstephan76/SM and checked-out branch v3.5; got {branch}, {origin}")
    revision = git("rev-parse", "HEAD", cwd=sm_repo)
    log(f"PASS SM branch v3.5: {revision}")
    return base


def sm_namespaces(sm_base):
    # Read only MESH_NAMESPACES from .env; do not parse credentials into Python.
    value = "istio-system bookinfo"
    envpath = sm_base / ".env"
    if envpath.is_file():
        for line in envpath.read_text(encoding="utf-8").splitlines():
            match = re.fullmatch(r'\s*MESH_NAMESPACES\s*=\s*[\"\']?([A-Za-z0-9 _.-]+)[\"\']?\s*', line)
            if match:
                value = match.group(1)
                break
    ordered = list(dict.fromkeys([*value.split(), "istio-system", "bookinfo", "full-multi"]))
    for name in ordered:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", name):
            raise Failure(f"Invalid mesh namespace in SM configuration: {name!r}")
    return " ".join(ordered)


def verify_images():
    images = (
        "ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-release-theme-v2",
        "ghcr.io/gstephan76/argo-sm-bookshelf:full-multi-crud-v5",
        "ghcr.io/gstephan76/argo-sm-mariadb:english-literature-20-v1",
        "ghcr.io/gstephan76/argo-sm-mariadb:classicos-brasileiros-20-v1",
    )
    for image in images:
        out = call(["skopeo", "inspect", "--no-creds", f"docker://{image}"], capture=True, timeout=90)
        obj = json.loads(out)
        if obj.get("Architecture") != "amd64" or obj.get("Os") != "linux":
            raise Failure(f"Unexpected image platform for {image}: {obj.get('Architecture')}/{obj.get('Os')}")
        log(f"PASS image {image}: {obj.get('Digest')}")


def assert_no_legacy_mariadb():
    raw = ocjson("-n", "full-multi", "get", "rollouts.argoproj.io")
    names = {x["metadata"]["name"] for x in raw.get("items", [])}
    bad = names & {"mariadb-a", "mariadb-b"}
    if bad:
        raise Failure(f"Legacy MariaDB Rollouts exist: {sorted(bad)}. Use protected migration, not fresh setup")


def check_rollout_manager():
    managers = ocjson("get", "rolloutmanagers.argoproj.io", "-A").get("items", [])
    ready = [m for m in managers if m.get("spec", {}).get("namespaceScoped") is False and
             m.get("status", {}).get("phase") == "Available" and
             m.get("status", {}).get("rolloutController") == "Available"]
    if ready:
        return
    if managers:
        if any(m.get("spec", {}).get("namespaceScoped") is False for m in managers):
            log("Cluster-scoped RolloutManager exists; waiting for controller")
        else:
            raise Failure("Only namespace-scoped RolloutManagers found; need cluster-scoped controller")
    else:
        call(["oc", "apply", "-f", "platform-monitoring/rollout-manager-cluster.yaml"], cwd=ROOT)
    deadline = time.monotonic() + 360
    while time.monotonic() < deadline:
        managers = ocjson("get", "rolloutmanagers.argoproj.io", "-A").get("items", [])
        for item in managers:
            s = item.get("status", {})
            if (item.get("spec", {}).get("namespaceScoped") is False and
                    s.get("phase") == "Available" and s.get("rolloutController") == "Available"):
                ns = item.get("metadata", {}).get("namespace", "openshift-gitops")
                deploy = maybe_oc_json("-n", ns, "get", "deployment", "argo-rollouts")
                if deploy and deploy.get("status", {}).get("availableReplicas", 0) >= 1:
                    log("PASS cluster-scoped Argo Rollouts controller Ready")
                    return
        time.sleep(5)
    raise Failure("Timed out waiting for cluster-scoped RolloutManager and controller")


def verify_db_volumes():
    names = [f"mariadb-data-mariadb-{app}-{track}-0" for app in APPS for track in TRACKS]
    pvs = []
    for name in names:
        pvc = ocjson("-n", "full-multi", "get", "pvc", name)
        phase = pvc.get("status", {}).get("phase")
        pv = pvc.get("spec", {}).get("volumeName")
        if phase != "Bound" or not pv:
            raise Failure(f"PVC {name} not Bound to a distinct volume: phase={phase}, volumeName={pv}")
        pvs.append(pv)
    if len(set(pvs)) != 4:
        raise Failure("HARD CONTRACT: A/B Blue/Green databases share one or more PVs")
    log("PASS all four independent PVCs Bound to distinct volumes")


def wait_gitops(revision, timeout=900):
    call(["oc", "-n", "openshift-gitops", "annotate", "application", "full-multi",
          "argocd.argoproj.io/refresh=hard", "--overwrite"], timeout=45)
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        app = ocjson("-n", "openshift-gitops", "get", "application", "full-multi")
        spec = app.get("spec", {}).get("syncPolicy", {}).get("automated", {})
        status = app.get("status", {})
        sync = status.get("sync", {})
        rev = sync.get("revision", "")
        phase = status.get("operationState", {}).get("phase", "")
        if phase in ("Error", "Failed") and status.get("operationState", {}).get("syncResult", {}).get("revision") == revision:
            raise Failure(f"Argo CD failed requested revision: {status.get('operationState', {}).get('message')}")
        if rev == revision:
            if sync.get("status") == "Synced":
                log(f"PASS GitOps revision {revision[:12]} Synced")
                return
            if spec.get("prune") is False and sync.get("status") == "OutOfSync":
                # Only acceptable during a protected legacy-object migration.
                log(f"INFO GitOps revision {revision[:12]} observed OutOfSync with prune=false; testing live specs and endpoints")
                return
        message = (rev[:12], sync.get("status"), phase)
        if message != last:
            log(f"WAIT GitOps revision={message[0]} sync={message[1]} operation={message[2]}")
            last = message
        time.sleep(5)
    raise Failure(f"Argo CD did not observe required revision {revision[:12]}")


def rollout(name):
    return ocjson("-n", "full-multi", "get", "rollouts.argoproj.io", name)


def env_value(obj, key):
    for cont in obj.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
        for value in cont.get("env", []):
            if value.get("name") == key:
                return value.get("value")
    return None


def blue_template(obj, name):
    marker = obj.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {}).get("demo-rollout-revision")
    if marker != "baseline-blue":
        return False
    app = name.split("-")[-1]
    if name.startswith("crud-"):
        return (obj.get("spec", {}).get("template", {}).get("metadata", {}).get("labels", {}).get("demo.crud-track") == "blue"
                and env_value(obj, "DB_TRACK") == "blue"
                and env_value(obj, "DB_HOST") == f"mariadb-{app}-blue.full-multi.svc.cluster.local")
    return env_value(obj, "MESSAGE_PREFIX") == f"Application {app.upper()} - BLUE"


def wait_blue_rollouts(timeout=900):
    deadline = time.monotonic() + timeout
    done = set()
    observed = {}
    while time.monotonic() < deadline:
        for app in APPS:
            for role in ROLES:
                name = f"{role}-{app}"
                if name in done:
                    continue
                r = rollout(name)
                if not blue_template(r, name):
                    raise Failure(f"{name}: refuses promotion because desired template is not verified Blue")
                meta, status = r.get("metadata", {}), r.get("status", {})
                phase = status.get("phase", "Unreconciled")
                current = (phase, status.get("currentStepIndex"), status.get("observedGeneration"))
                if observed.get(name) != current:
                    log(f"WAIT {name}: phase={phase}, step={current[1]}, observedGen={current[2]}/{meta.get('generation')}")
                    observed[name] = current
                if phase == "Degraded":
                    raise Failure(f"{name}: Degraded; examine events/analysis and correct root cause before retrying")
                if status.get("observedGeneration", 0) < meta.get("generation", 1):
                    continue
                if phase == "Paused":
                    if role != "crud" and status.get("currentStepIndex") != 6:
                        raise Failure(f"{name}: unexpected Canary pause step {status.get('currentStepIndex')}; refusing auto promotion")
                    call(["oc", "argo", "rollouts", "promote", name, "-n", "full-multi"], timeout=60)
                elif (phase == "Healthy" and status.get("stableRS") and
                      status.get("stableRS") == status.get("currentPodHash") and
                      status.get("availableReplicas", 0) >= r.get("spec", {}).get("replicas", 1)):
                    done.add(name)
                    log(f"PASS {name}: Blue Healthy")
        if len(done) == 6:
            return
        time.sleep(5)
    raise Failure(f"Timeout waiting for all six Healthy Blue Rollouts: {sorted(done)}")


def abort_pending_rollouts():
    for app in APPS:
        for role in ROLES:
            name = f"{role}-{app}"
            r = rollout(name)
            status = r.get("status", {})
            phase = status.get("phase")
            if phase == "Degraded" and not status.get("abort"):
                raise Failure(f"{name}: degraded without known abort; diagnose before rebaseline")
            if phase in ("Paused", "Progressing") and status.get("stableRS") and (
                    status.get("stableRS") != status.get("currentPodHash")):
                log(f"ABORT candidate {name}: {phase}. This is the requested reset to Blue")
                call(["oc", "argo", "rollouts", "abort", name, "-n", "full-multi"], timeout=60)


def commit_blue():
    # This is the only stage that can write application Git; do not stage other work.
    target_paths = [f"full-multi/rollout-{role}-{app}.yaml" for app in APPS for role in ROLES]
    for app in APPS:
        call([sys.executable, "scripts/full-multi-spec.py", "set", "--instance", app,
              "--color", "blue"], cwd=ROOT, timeout=45)
    call(["git", "add", "--", *target_paths], cwd=ROOT)
    call(["git", "diff", "--cached", "--check"], cwd=ROOT)
    stage = git("diff", "--cached", "--name-only")
    unknown = set(stage.splitlines()) - set(target_paths)
    if unknown:
        raise Failure(f"Unexpected staged files after Blue rebaseline: {unknown}")
    if stage:
        call(["git", "commit", "-m", "Prepare full-multi: deterministic Blue baseline"], cwd=ROOT)
        call(["git", "push", "origin", "main"], cwd=ROOT, timeout=180)
    else:
        log("PASS Git desired templates already Blue; no commit necessary")
    return git("rev-parse", "HEAD")


def baseline_e2e():
    """Check HTTP -> Front -> Backend -> CRUD -> each A/B English DB."""
    route = ocjson("-n", "full-multi", "get", "route", "full-multi")
    host = route.get("spec", {}).get("host", "")
    if not host:
        raise Failure("Route/full-multi lacks .spec.host")
    for app in APPS:
        headers = ["-H", f"x-app-target: {app}", "-H", "x-front-track: stable",
                   "-H", "x-backend-track: stable", "-H", "x-db-track: blue"]
        raw = call(["curl", "-q", "-kfsS", "--connect-timeout", "5", "--max-time", "20",
                    *headers, f"https://{host}/api/books"], capture=True, timeout=30)
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise Failure(f"Application {app}: invalid JSON response") from exc
        expected_host = f"mariadb-{app}-blue.full-multi.svc.cluster.local"
        checks = {
            "application": app.upper(),
            "database_track": "blue",
            "database_host": expected_host,
            "frontend_release": "BLUE",
            "backend_release": "BLUE",
        }
        for key, val in checks.items():
            if data.get(key) != val:
                raise Failure(f"App {app}: expected {key}={val!r}, got {data.get(key)!r}")
        books = data.get("books") or []
        if len(books) != 20 or books[0].get("title") != "Pride and Prejudice":
            raise Failure(f"App {app}: wrong English data set: count={len(books)}, first={books[0] if books else None}")
        page = call(["curl", "-q", "-kfsS", "--connect-timeout", "5", "--max-time", "20",
                     *headers, f"https://{host}/"], capture=True, timeout=30)
        if "Pride and Prejudice" not in page:
            raise Failure(f"App {app}: HTML homepage did not render English books")
        log(f"PASS E2E application {app.upper()}: Blue Front -> Backend -> CRUD -> dedicated MariaDB")
    verify_db_volumes()
    call(["bash", "scripts/full-multi-db-readonly.sh", "--verify"], cwd=ROOT, timeout=300)


def ensure_gitops_app():
    current = maybe_oc_json("-n", "openshift-gitops", "get", "application", "full-multi")
    if current:
        source = current.get("spec", {}).get("source", {})
        if (source.get("path") != "full-multi" or
                source.get("targetRevision") != "main" or
                "gstephan76/argo-sm" not in source.get("repoURL", "")):
            raise Failure("Existing Argo CD Application has a different source; refuse takeover")
    call(["oc", "apply", "-f", "argocd/application-full-multi.yaml"], cwd=ROOT, timeout=90)
    return git("rev-parse", "HEAD")


def setup(args):
    sm = check_sm_repo(args.sm_repo)
    assert_git_clean()
    preflight_local()
    verify_images()
    log("SM v3.5: preinstalled OLM Operators required; missing CSVs/CRDs will fail in check-prereqs.sh")
    # Bootstrap init-env does not overwrite tenant UUID or credentials.
    call(["bash", str(sm / "scripts/init-env.sh")], cwd=sm, timeout=60)
    # Namespace must exist when SM applies proxy PodMonitor for full-multi.
    call(["oc", "apply", "-f", "full-multi/namespace.yaml"], cwd=ROOT, timeout=45)
    env = os.environ.copy()
    env["MESH_NAMESPACES"] = sm_namespaces(sm)
    env["RUN_E2E"] = "1"  # SM validates its own Bookinfo + traces + metrics
    env["DEPLOY_BOOKINFO"] = "1"
    env["BOOTSTRAP_MCG"] = "1" if args.bootstrap_mcg else "0"
    # The default v3.5 mesh-base behavior on fresh cluster is explicit opt-in.
    mesh_exists = maybe_oc_json("-n", "istio-system", "get", "istio", "default")
    env["APPLY_MESH_BASE"] = "0" if mesh_exists else "1"
    if args.bootstrap_mcg:
        log("INFO ODF standalone MCG gp3-csi bootstrap enabled (AWS-specific)")
    else:
        log("INFO MCG bootstrap disabled; existing NooBaa must be healthy")
    call(["bash", str(sm / "scripts/apply.sh")], cwd=sm, env=env, timeout=5400)
    call(["bash", str(sm / "scripts/postcheck.sh")], cwd=sm, env=env, timeout=1800)
    # The SM stack does not install OpenShift GitOps or its Rollouts operator.
    check_rollout_manager()
    assert_no_legacy_mariadb()
    call(["bash", "scripts/full-multi-secrets.sh"], cwd=ROOT, timeout=60)
    call([sys.executable, "scripts/full-multi-bootstrap.py", "preflight"], cwd=ROOT, timeout=80)
    call(["bash", "scripts/full-multi-databases.sh", "--apply"], cwd=ROOT, timeout=3000)
    verify_db_volumes()
    call(["bash", "scripts/full-multi-db-readonly.sh", "--apply"], cwd=ROOT, timeout=600)
    call(["bash", "scripts/full-multi-db-readonly.sh", "--verify"], cwd=ROOT, timeout=600)
    call(["oc", "kustomize", str(ROOT / "full-multi")], capture=True, timeout=60)
    # Server-side dry-run with no secret contents printed.
    manifest = call(["oc", "kustomize", str(ROOT / "full-multi")], capture=True, timeout=60)
    checked = subprocess.run(["oc", "apply", "--dry-run=server", "-f", "-"], input=manifest,
                             text=True, errors="replace", capture_output=True, timeout=150)
    if checked.returncode:
        raise Failure("Demo manifests rejected by API: " + checked.stderr[-3000:])
    # Fresh-cluster GitOps should first deploy Blue, never the repository's
    # potentially paused Yellow demonstration template. An existing Argo CD
    # Application is reset by rebaseline() after its source is verified.
    if maybe_oc_json("-n", "openshift-gitops", "get", "application", "full-multi") is None:
        log("Fresh deployment: publishing Blue baseline before creating Argo CD Application")
        commit_blue()
    ensure_gitops_app()
    wait_gitops(git("rev-parse", "HEAD"))
    rebaseline(args, verified=True)
    log("PASS full-multi global setup: SM v3.5 E2E + all six Blue Rollouts + four SQL databases")


def rebaseline(args, *, verified=False):
    if not verified:
        assert_git_clean()
        preflight_local()
    call(["oc", "argo", "rollouts", "version"], timeout=35)
    call([sys.executable, "scripts/full-multi-bootstrap.py", "preflight"], cwd=ROOT, timeout=70)
    verify_db_volumes()
    # Reconcile SELECT-only database permissions before resetting the Rollouts.
    call(["bash", "scripts/full-multi-db-readonly.sh", "--apply"], cwd=ROOT, timeout=500)
    call(["bash", "scripts/full-multi-db-readonly.sh", "--verify"], cwd=ROOT, timeout=500)
    abort_pending_rollouts()
    target = commit_blue()
    wait_gitops(target)
    wait_blue_rollouts(timeout=args.timeout)
    baseline_e2e()
    # Completed history is discarded only AFTER all six stable Blue revisions
    # are Healthy. Never delete active/preview ReplicaSets or a database PVC.
    for app in APPS:
        call([sys.executable, "scripts/full-multi-history-reset.py", "--instance", app,
              "--timeout-seconds", str(args.timeout)], cwd=ROOT, timeout=args.timeout + 120)
    baseline_e2e()
    log("PASS GLOBAL PREPARE: histories cleared; Blue baseline Healthy for A+B; four isolated SQL databases unchanged; ready for start")


def paused_e2e():
    verify_db_volumes()
    call(["bash", "scripts/full-multi-db-readonly.sh", "--verify"], cwd=ROOT, timeout=500)
    call([sys.executable, "scripts/full-multi-crud-pause-gate.py", "--instance", "all"], cwd=ROOT, timeout=100)
    call(["bash", "scripts/full-multi-validate-data-paths.sh", "--preview"], cwd=ROOT, timeout=180)
    for app in APPS:
        for role in ("front", "backend"):
            item = rollout(f"{role}-{app}")
            phase = item.get("status", {}).get("phase")
            step = item.get("status", {}).get("currentStepIndex")
            if phase != "Paused" or step != 6:
                raise Failure(f"{role}-{app}: expected Canary 50% pause (phase={phase}, step={step})")
    log("PASS E2E PAUSED: Front/Backend 50%, CRUD Blue active + Green preview for both independent applications")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("setup", "prepare", "e2e", "status"))
    parser.add_argument("--sm-repo", type=Path, default=ROOT.parent / "ServiceMesh",
                        help="Checkout of gstephan76/SM branch v3.5 (default: ../ServiceMesh)")
    parser.add_argument("--bootstrap-mcg", action="store_true",
                        help="Explicitly allow the AWS gp3-csi ODF/MCG bootstrap from SM v3.5")
    parser.add_argument("--apply", action="store_true", help="Allow writes to cluster and/or Git")
    parser.add_argument("--push", action="store_true", help="Authorize GitOps main commits and push")
    parser.add_argument("--state", choices=("blue", "paused"), default="blue", help="E2E target state")
    parser.add_argument("--timeout", type=int, default=1200)
    args = parser.parse_args(argv)
    if args.timeout < 60:
        parser.error("--timeout must be >=60 seconds")
    if args.command in ("setup", "prepare") and not (args.apply and args.push):
        stages = ("SM v3.5 prereqs -> apply.sh (19 stages) -> four DBs -> GitOps -> Blue rebaseline -> E2E -> history reset"
                  if args.command == "setup" else
                  "verify prerequisites -> abort pending candidates -> GitOps Blue rebaseline -> E2E -> delete completed histories")
        log(f"PLAN ONLY ({args.command}): {stages}")
        log("No changes made. Re-run with --apply --push to authorize changes.")
        return 0
    if args.command in ("e2e", "status") and args.apply:
        parser.error("e2e/status are read-only; do not pass --apply")
    try:
        if args.command == "setup":
            setup(args)
        elif args.command == "prepare":
            rebaseline(args)
        elif args.command == "e2e":
            baseline_e2e() if args.state == "blue" else paused_e2e()
        else:
            call(["oc", "-n", "full-multi", "get", "rollouts.argoproj.io"])
            call(["oc", "-n", "full-multi", "get", "statefulsets,pvc"])
            call(["oc", "-n", "openshift-gitops", "get", "application", "full-multi"])
        return 0
    except Failure as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
