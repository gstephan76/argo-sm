# Current Frontend/Backend release theme

`full-multi/configmap-release-theme.yaml` supplies `theme.json`. Frontend and
Backend show the actual serving revision color: BLUE baseline or YELLOW
candidate. This styling is **not** the Blue/Green language of CRUD, which is
an independent Argo Rollouts BlueGreen strategy. A Blue Frontend can call a
Green CRUD preview when the `x-db-track: green` HTTP header is supplied.

The projected ConfigMap updates without image rebuild; Go reads its contents
per request and validates strict CSS hex colors. `MESSAGE_PREFIX` is a
pod-template revision identity, not a client-header inference.

Current build script for Frontend/Backend:

```bash
bash scripts/build-push-full-multi.sh
```

The native lifecycle is documented in [README.md](README.md). `prepare`
sets Blue; `start` creates YELLOW Canary revisions and a GREEN CRUD preview;
`promote` explicitly advances all three tiers but is **not** used during the
paused demonstration. MariaDB is seeded once and cannot be affected by theme
changes.
