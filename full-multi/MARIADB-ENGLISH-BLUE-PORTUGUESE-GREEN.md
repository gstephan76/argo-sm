# MariaDB datasets: BLUE English / GREEN Brazilian Portuguese

## Required contract

| Track | Argo Rollouts Service | Source and image | Fresh dataset |
|---|---|---|---|
| BLUE | `mariadb-{a,b}-stable` | `mariadb-english/` / `english-literature-20-v1` | 20 English-language classics |
| GREEN | `mariadb-{a,b}-canary` | `mariadb-classics/` / `classicos-brasileiros-20-v1` | 20 Brazilian Portuguese classics |

The image is selected by the lifecycle CLI: `prepare` uses BLUE's English
image; `start` uses GREEN's Brazilian image. `prepare` / `start` also alter the
Front/Backend Rollouts: consult `scripts/full-multi.sh` before running them.

**The dataset is only seeded at database-directory initialization.** The two
MariaDB pods use independent `emptyDir` volumes. Updating a pod-template image
cannot change a running pod's existing database contents. Because
`autoPromotionEnabled: false`, the revised pod starts as a preview; only an
explicit promotion changes the active database. That promotion can discard
the old active pod's `emptyDir` data. **Use this workflow only with disposable
demo data.** Do not use it for production databases.

## Controlled rollout

1. Run the installer from the repo root. Do not commit until image publishing
   succeeds. Review the changes with `git diff`.
2. Publish BLUE's English image:

   ```bash
   bash scripts/build-push-mariadb-english.sh
   skopeo inspect --no-creds docker://ghcr.io/gstephan76/argo-sm-mariadb:english-literature-20-v1
   ```

   The existing Brazilian GREEN image must also be pullable:

   ```bash
   skopeo inspect --no-creds docker://ghcr.io/gstephan76/argo-sm-mariadb:classicos-brasileiros-20-v1
   ```

3. Validate the source tree, then commit and push. The current manifest
   rewrite may trigger an English *preview* in an application currently at
   `baseline-blue`, without promoting it. Inspect the live Rollouts. The
   actual BLUE database will remain the old image until deliberately promoted.
4. For each application individually, during an approved demo reset:

   ```bash
   INSTANCE=a bash scripts/full-multi.sh prepare
   # Confirm the active MariaDB image and titles before starting GREEN:
   INSTANCE=a bash scripts/full-multi.sh start
   ```

   Repeat for B when ready. `prepare` may promote a MariaDB preview and
   advance Front/Backend rollouts. Do not invoke this while protecting active
   data, running analyses, or a canary you intend to preserve.
5. With both tracks present, confirm `mariadb-a-stable` and
   `mariadb-a-canary` have different ready pod names via EndpointSlices,
   and validate the first books by HTTP:

   ```bash
   HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
   for app in a b; do
      for track in blue green; do
        echo "=== app=$app db=$track ==="
        curl -kfsS -H "x-app-target: $app" \
          -H 'x-front-track: stable' -H 'x-backend-track: stable' \
          -H "x-db-track: $track" "https://${HOST}/api/books" |
          jq '{database_track,database_host,books: [.books[0:3][] | .title]}'
      done
   done
   ```

   Expected first BLUE title: `Pride and Prejudice`.
   Expected first GREEN title: `Memórias Póstumas de Brás Cubas`.

No existing MariaDB pod is automatically rewritten, promoted, or deleted by
this installer; only files in the working copy are changed.
