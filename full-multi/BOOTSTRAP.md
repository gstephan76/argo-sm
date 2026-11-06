# full-multi bootstrap

Canonical topology and commands: [README.md](README.md).

- Six Argo Rollouts: Frontend/Backend Canary A/B, CRUD BlueGreen A/B.
- Four independent MariaDB StatefulSets (A-Blue, A-Green, B-Blue, B-Green),
  each with its own 4 GiB RWO PVC and 20 seeded titles.
- `scripts/full-multi-secrets.sh` provisions A/B credentials only once; it
  never overwrites or rotates an existing Secret.
- `scripts/full-multi-databases.sh --apply` stages all four persistent DBs.
- `scripts/full-multi-db-readonly.sh --apply` restricts the application SQL
  account to SELECT after the first seed.
- `scripts/full-multi-spec.py verify` checks local manifests.
- `scripts/full-multi.sh deploy` applies the Argo CD Application, **including
  its declared automatic pruning**. Never invoke deploy during a protected
  migration where obsolete MariaDB Rollouts must remain.
- `prepare` establishes Blue, `start` creates a paused Green preview,
  and `promote` is an explicit user action. No automatic CRUD promotion.

After `start`, check both apps with `scripts/full-multi-crud-pause-gate.py`
and `scripts/full-multi-validate-data-paths.sh --preview`.
