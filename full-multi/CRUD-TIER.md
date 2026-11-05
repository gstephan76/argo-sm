# CRUD tier: BlueGreen rollout, immutable language/database paths

The authoritative installation and migration guide is [README.md](README.md).

Each `crud-a` / `crud-b` Rollout has `blueGreen` strategy, with `activeService`
and `previewService` managed by Argo Rollouts. The BLUE pod revision binds
`DB_HOST=mariadb-{a,b}-blue`, containing English titles. The GREEN revision
binds `DB_HOST=mariadb-{a,b}-green`, containing Portuguese titles.
Each DB is an independent MariaDB StatefulSet/PVC, not a Rollout.

`x-db-track: blue|stable` routes to fixed BLUE CRUD pods;
`x-db-track: green|canary` routes to fixed GREEN CRUD pods. Omitting the
header selects the promoted `crud-{a,b}-active` Service. After promotion,
the old fixed-color route will cease to work once its ReplicaSet scales to
zero. Do not confuse the frontend/backend BLUE/YELLOW canary colors with
the CRUD BLUE/GREEN language/database choices.
