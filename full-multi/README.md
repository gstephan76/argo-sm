# full-multi — two isolated applications, six Rollouts, four MariaDB databases

This is the **current** OpenShift GitOps topology. Application A and B use the
same demonstration code and the same book catalogs, but **never share** a database,
StatefulSet, Service, PVC or SQL connection. Frontend/Backend have independent
Argo Rollouts Canary strategies, while CRUD-A/B have native Argo Rollouts
BlueGreen strategies.

## Contract

| Application | HTTP Rollouts | CRUD Rollout | SQL database (own 4Gi RWO PVC) |
|---|---|---|---|
| A | `front-a`, `backend-a` (Canary) | `crud-a` Blue active | `mariadb-a-blue` — 20 English books |
| A | same | `crud-a` Green preview | `mariadb-a-green` — 20 Portuguese books |
| B | `front-b`, `backend-b` (Canary) | `crud-b` Blue active | `mariadb-b-blue` — 20 English books |
| B | same | `crud-b` Green preview | `mariadb-b-green` — 20 Portuguese books |

Each MariaDB is a separate StatefulSet with its own PVC. Seed data loads on
first initialization only. The application uses a `bookapp` account restricted
to SELECT on `bookdb.books` (see `scripts/full-multi-db-readonly.sh`). Do not
reset the PVCs during the demonstration.

## Paused demonstration state: no time limit

`INSTANCE=a|b bash scripts/full-multi.sh start` creates a Green preview while
Blue remains active and available. CRUD **must remain Paused** for the full
session. `autoPromotionEnabled: false`, `previewReplicaCount: 1` and the
absence of `autoPromotionSeconds` prevent timed advancement. While paused,
`scaleDownDelaySeconds: 300` does *not* apply: it only controls old ReplicaSet
scale-down **after a user explicitly promotes**. Both fixed routes must have
ready endpoints for as long as the paused demo runs.

Istio routing: `x-db-track: blue|stable` selects the **Blue CRUD pod** and its
English database; `x-db-track: green|canary` selects the **Green CRUD pod** and
its Portuguese database. Without `x-db-track`, Istio uses `crud-{a,b}-active`.
`crud-{a,b}-preview` is managed by Argo Rollouts. Blue/Green fixed Services
select pods by immutable `demo.crud-track`, and Argo Rollouts manages the
`rollouts-pod-template-hash` on active/preview Services. The Backend connects
to a *routing host* named `crud-{a,b}-blue` and passes headers; it does not
connect to SQL directly. No request header can change an individual CRUD Pod's
`DB_HOST` or `DB_TRACK`.

Frontend and Backend Canary tracks are independent from the CRUD language:
`x-front-track: stable|canary`, `x-backend-track: stable|canary`.

## Runbook

Prerequisites: the Argo Rollouts controller and CLI, OpenShift credentials,
existing `db-credentials-a/b` Secrets, GitHub access, default StorageClass,
and three public OCI images (`full-multi-crud-v5`, English 20, Portuguese 20).
The four MariaDB databases are already installed in the migration cluster.

```bash
cd ~/Documents/POCs/argo-sm
python3 scripts/full-multi-spec.py verify
(cd full-multi/bookshelf && go test -count=1 ./...)
# Only for a new environment; creates 4 StatefulSets and four independent PVCs:
bash scripts/full-multi-databases.sh --apply
# After the initial 20-book seed, enforce table-scoped application privileges:
bash scripts/full-multi-db-readonly.sh --apply
bash scripts/full-multi-db-readonly.sh --verify
```

For **each** application, run `prepare` then `start` once:

```bash
INSTANCE=a bash scripts/full-multi.sh prepare
INSTANCE=b bash scripts/full-multi.sh prepare
# Start both applications and run the complete pause + four-database gate automatically.
bash scripts/full-multi.sh start
```

`prepare` establishes a Blue active baseline. If a previous Green revision was
promoted, resetting Blue in `prepare` is an explicit *pre-demo* rebaseline, and
may require promotion to finish. Do **not** execute `prepare` during a paused
**Green** preview. A temporary Blue-to-Blue image update may itself be paused;
`prepare` can deliberately finish that pre-demo Blue rebaseline. `start` creates
the Green candidate, waits for both Rollouts to pause
and checks the selected application's Blue/Green HTTP and SQL paths.

**Remain paused indefinitely for the demonstration.** Re-run the read-only
validation whenever desired; there is no timer-driven promotion.

`promote` remains a fully supported **explicit command** that advances CRUD,
Backend and Frontend. It is **not run during the paused demo**:

```bash
# Only when you deliberately want to finish a rollout, outside the demo:
INSTANCE=a bash scripts/full-multi.sh promote
INSTANCE=b bash scripts/full-multi.sh promote
```

After a deliberate promotion, the old Blue CRUD ReplicaSet can be scaled down
after `scaleDownDelaySeconds`. Fixed Blue and Green endpoints are both guaranteed
*only while the matching Blue-active/Green-preview revisions coexist*. To reset
for another demo, run `prepare` and `start` again.

For user-visible data-path verification:

```bash
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
for app in a b; do
  for db in blue green; do
    curl -kfsS -H "x-app-target: $app" -H 'x-front-track: stable' \
      -H 'x-backend-track: stable' -H "x-db-track: $db" \
      "https://$HOST/api/books" | jq '{application,database_track,database_host,count:(.books|length),first:.books[0].title}'
  done
done
```

### GitOps and migration

`argocd/application-full-multi.yaml` tracks `main` with `prune: true`, intended
for the *final* six-Rollout topology. While retaining the two old ephemeral
MariaDB Rollouts during a protected transition, **keep live pruning disabled**.
Do not run `full-multi.sh deploy` or reapply the Application YAML until the
cutover and required cleanup are explicitly completed. No old database data
needs migrating. Application data is disposable seed data, but PVC ownership
and separation are hard requirements. `scripts/full-multi-databases.sh` stages
only the four new DB resources without changing old ones.

### Code ownership and maintenance

- `scripts/full-multi.sh` manages all three Rollout tiers (`prepare`, `start`,
  `promote`, `status`, `cleanup`, `traffic`).
- `scripts/full-multi-spec.py` asserts six Rollouts, four databases, and
  disabled CRUD auto-promotion.
- `scripts/full-multi-crud-pause-gate.py` validates **real** paused Blue/Green
  pods, DB host bindings, active/preview endpoint membership, and independent PVs.
- `scripts/full-multi-validate-data-paths.sh --preview` verifies both A/B paths
  for both languages via HTTP (20 books and correct dataset).
- `scripts/full-multi-db-readonly.sh` enforces and verifies SELECT-only access.

The previous eight-Rollout MariaDB BlueGreen instructions and one-shot
observability installer were deleted. Observability is configured using the
current resources and instructions in `OBSERVABILITY.md`.
