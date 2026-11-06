# Canary at 50% and CRUD BlueGreen paused preview

There are **four** Istio-managed Canary Rollouts: `front-a/b`, `backend-a/b`.
They progress 10% → 25% → 50%, running the corresponding AnalysisRun at
each step, then pause for explicit promotion. Each application also has a
separate CRUD BlueGreen Rollout, paused with Blue active and Green preview.
MariaDB uses four persistent **StatefulSets** and is never an Argo Rollout.

The front/backend canaries and the CRUD BlueGreen preview are both created by:

```bash
INSTANCE=a bash scripts/full-multi.sh start
INSTANCE=b bash scripts/full-multi.sh start
python3 scripts/full-multi-crud-pause-gate.py --instance all
bash scripts/full-multi-validate-data-paths.sh --preview
```

`x-front-track: stable|canary` and `x-backend-track: stable|canary` force
Front/Backend revision selection. `x-db-track: blue|green` independently
selects the English or Portuguese CRUD Pod/DB pair.

**No promotion is required during the demo.** The two CRUD revisions remain
available as long as the Rollout is paused. To advance all three Rollout tiers
*deliberately* after the demo, run `INSTANCE=a|b bash scripts/full-multi.sh promote`.
