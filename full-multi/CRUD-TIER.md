# CRUD BlueGreen lifecycle

Each application has **one** CRUD Rollout (`crud-a` or `crud-b`), with Blue
active and Green preview after `start`. Both versions use the same v5
read-only image but different pod-template `DB_HOST`/`DB_TRACK`, and distinct
StatefulSet/PVC-backed MariaDB instances.

The controller pauses the new Green ReplicaSet indefinitely because
`autoPromotionEnabled: false`; it runs one full preview replica using
`previewReplicaCount: 1`. `autoPromotionSeconds` MUST be absent. No implicit
`promote` takes place in `start`, `status` or data-path validation.

The user can **explicitly** run `INSTANCE=a|b bash scripts/full-multi.sh promote`
which promotes CRUD along with Frontend and Backend. In that case Blue can
scale down after the configured 300-second delay. The demo intentionally does
not invoke promote, so both languages remain available throughout.

`x-db-track: blue|stable` routes to the Blue CRUD Pod; `green|canary` to the
Green Pod; without a header, `crud-{a,b}-active`. Argo Rollouts owns only the
active/preview Service template-hash selectors; fixed Blue/Green Services
select `demo.crud-track` labels. A/B use distinct credential Secret objects and must not share SQL endpoints
or PVCs. Validate before and during the demo:

```bash
python3 scripts/full-multi-crud-pause-gate.py --instance all
bash scripts/full-multi-validate-data-paths.sh --preview
bash scripts/full-multi-db-readonly.sh --verify
```
