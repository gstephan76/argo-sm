# full-multi bootstrap

Canonical topology and commands: [README.md](README.md).

- `scripts/full-multi prepare` is the complete installation command: it
  reconciles OVN and GitOps/Rollouts prerequisites, provisions missing demo
  credentials and databases, deploys GitOps and establishes both Blue apps.
- Six Argo Rollouts: Frontend/Backend Canary A/B, CRUD BlueGreen A/B.
- Four independent MariaDB StatefulSets (A-Blue, A-Green, B-Blue, B-Green),
  each with its own 4 GiB RWO PVC and 20 seeded titles.
- `scripts/full-multi-secrets.sh` provisions A/B credentials only once; it
  never overwrites or rotates an existing Secret.
  With `--generate-demo`, a partial first provision can create only the missing
  app's Secret before that app has workloads or PVCs. Missing credentials for
  existing data require restoration; they are never regenerated automatically.
- `scripts/full-multi-databases.sh --apply` stages all four persistent DBs.
- `scripts/full-multi-db-readonly.sh --apply` restricts the application SQL
  account to SELECT after the first seed.
  It first establishes and checks direct table-level SELECT, then revokes
  recognized excess privileges by scope. Unknown grants are refused for manual
  inspection; account changes across the four databases are not atomic.
- `scripts/full-multi-spec.py verify` checks local manifests.
- `scripts/full-multi.sh deploy` applies the Argo CD Application, **including
  its declared automatic pruning** for the separate `multi-ambient` Application.
- `prepare` establishes Blue, `start` creates a paused Green preview,
  and `promote` is an explicit user action. No automatic CRUD promotion.

After `start`, check both apps with `scripts/full-multi-crud-pause-gate.py`
and `scripts/full-multi-validate-data-paths.sh --preview`.
