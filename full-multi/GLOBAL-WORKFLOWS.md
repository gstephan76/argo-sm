# full-multi: global setup and prepare

## Authoritative repositories

- Demo: `https://github.com/gstephan76/argo-sm`, branch `main`.
- Service Mesh: `https://github.com/gstephan76/SM`, **branch `v3.5`**, installation directory **`v3/v3.5/`**. No OSSM 3.4 baseline scripts are invoked.

The Service Mesh 3.5 installer performs 19 stages (OSSM 3.5, user workload monitoring, ODF/NooBaa S3, Tempo, OpenTelemetry, Kiali, console integrations, Bookinfo and its end-to-end checks). `MESH_NAMESPACES` includes `full-multi` so proxy monitoring covers the demo as well. SM's existing `.env` and its `TEMPO_TENANT_ID` are preserved.

**Operator prerequisite:** `SM/v3/v3.5/scripts/check-prereqs.sh` requires already-installed supported Red Hat Operators and their CRDs (OSSM 3.5, Kiali, OTel, Tempo, Cluster Observability when UI is enabled). The ODF operator can be installed via SM's AWS-specific standalone MCG bootstrap. OpenShift GitOps with Rollouts CRDs and an `openshift-gitops` namespace is also required. The global script verifies these prerequisites, but does not fabricate Operator subscriptions or alter incompatible cluster-wide installations. It will fail with actionable errors until they are present.

## Installation

Install using `python3 install_global_workflows.py --repo . --check` followed by
`--apply` from this bundle. This copies the two wrappers, Python controller,
unit tests and documentation, and patches `scripts/full-multi.sh` to make
`start` cover both A/B by default. Review and commit these changes before
running the workflows (their GitOps preflight refuses dirty tracked files).

Ensure the SM checkout uses the right branch:

```bash
cd ~/Documents/POCs/ServiceMesh
git fetch origin v3.5
git switch v3.5
git pull --ff-only origin v3.5
```

The existing SM `.env` is deliberately not deleted or overwritten. New installations may use `v3/v3.5/scripts/init-env.sh`, which generates a tenant UUID once.

## Setup from a new OCP 4.22 AWS cluster

```bash
cd ~/Documents/POCs/argo-sm
bash scripts/full-multi-global-setup.sh
bash scripts/full-multi-global-setup.sh --apply --push --bootstrap-mcg \
  --sm-repo ~/Documents/POCs/ServiceMesh
```

`--bootstrap-mcg` is specifically for the `gp3-csi`/ODF standalone MCG path described in SM v3.5. Omit it when compatible ODF/NooBaa is already healthy. **It does not silently replace an existing storage system.**

The setup script validates GitOps and cluster permissions, initializes the SM environment without overwriting its tenant, adds `full-multi` to monitored mesh namespaces, executes the upstream v3.5 installer and its native Bookinfo/Tempo/Prometheus E2E, checks Argo Rollouts, provisions and isolates four MariaDB StatefulSets/PVCs and readonly grants, deploys the `full-multi` Argo CD Application, establishes a healthy Blue baseline in GitOps, resets retired Rollout history and runs A/B Bookshelf HTTP-to-SQL tests.

For a fresh deployment, the following secrets must already be present in the environment if `db-credentials-a/b` are missing. The helper does not overwrite existing Secrets and never prints these values:

```bash
export FULL_MULTI_DB_PASSWORD='...'
export FULL_MULTI_DB_ROOT_PASSWORD='...'
```

**Security:** Avoid typing real passwords into shared terminals or retaining them in shell history. Use your usual secure secret-injection mechanism. `FULL_MULTI_DB_*` are only needed for first provisioning.

## Global prepare: any *valid* Rollout lifecycle state

```bash
cd ~/Documents/POCs/argo-sm
bash scripts/full-multi-global-prepare.sh
bash scripts/full-multi-global-prepare.sh --apply --push
```

This is a **deliberate reset**, not a status check:

1. Verify the GitOps source, service controller, four isolated PVCs and SELECT-only application privileges.
2. Abort any in-progress/paused candidate revisions (the application rolls back toward its previously healthy active ReplicaSet). Does not delete data.
3. Set all six Rollout templates to deterministic Blue/English baseline via `scripts/full-multi-spec.py set`, commit and push only six Rollout YAMLs to `main` when necessary.
4. Observe GitOps revision, and complete **only confirmed Blue** pauses with native Argo Rollouts promotion. Green/YELLOW candidates are never promoted as part of a prepare reset.
5. Require all six Blue Rollouts Healthy and test the A/B read-only HTTP-to-SQL data paths.
6. Delete completed AnalysisRuns and non-current ReplicaSets with the existing strict history reset implementation; leave the four StatefulSets/PVCs intact.
7. Re-run E2E. Success means ready for `start` on both applications.

The reset handles previously paused Green, promoted Green, paused Canary, Healthy Blue, and previously aborted updates **when the cluster is otherwise functional**. It fails closed on unrecognized Degraded state, missing controller, bad storage, Git divergence or unhealthy infrastructure: no software can safely promise to recover arbitrary failures by silently deleting resources.

Do not run global prepare during a demonstration if you intend to keep the Green preview paused; `prepare` intentionally returns to Blue only.

## Start the demo and verify it stays paused

```bash
# One command starts both A and B, and automatically runs the paused CRUD
# gate, four-PVC isolation check, and Blue/Green HTTP-to-SQL E2E checks.
bash scripts/full-multi.sh start
```

No separate `full-multi-crud-pause-gate.py` command is necessary: the per-app
checks run inside each `start` operation, and the final cross-app gate runs
inside the combined `start` entry point. `INSTANCE=a|b bash scripts/full-multi.sh
start` remains available for individual-app diagnostics.

All four SQL databases remain independently available and persistent. Both CRUD Rollouts must be Paused, with Blue active/Green preview serving both 20-book datasets for the entire demonstration. `promote` still works on CRUD, Front and Backend **only when explicitly invoked**:

```bash
INSTANCE=a bash scripts/full-multi.sh promote
INSTANCE=b bash scripts/full-multi.sh promote
```

After a deliberate promotion, Blue endpoints may be scaled down; global `prepare` then restores the Blue baseline for the next demonstration.

## Read-only status / verification

```bash
python3 scripts/full-multi-global.py status
python3 scripts/full-multi-global.py e2e --state blue
python3 scripts/full-multi-global.py e2e --state paused
python3 -m unittest discover -s tests -p test_full_multi_global.py
```

There is no automatic GitHub write action through ChatGPT. After reviewing the installed scripts, commit and push them using your own `git` credentials. **Never commit the SM `.env`, generated Tempo secrets, or database credentials.**
