# MariaDB BLUE/GREEN preview: 20 Brazilian literary classics

This is a **separate MariaDB 10.11 image** for the **GREEN preview** of the
existing `Rollout/mariadb-a` and `Rollout/mariadb-b`. It is not an HTTP canary
and does not introduce percentage-based SQL traffic routing.

- Existing **BLUE active Pod**: keeps its current image and data until promotion.
- New **GREEN preview Pod**: loads 20 curated Brazilian literary classics on
  first initialization into `bookdb.books` (`id`, `title`, `author`, `year`).
- Both active/preview Services remain `mariadb-{a,b}-stable` and
  `mariadb-{a,b}-canary`, respectively. The `-canary` name here denotes
  the **blue-green preview Service**, not a MariaDB canary strategy.
- The Go Backend's `INSERT IGNORE` sample inserts use IDs 1–3, deliberately
  already seeded here, so querying the preview preserves **exactly 20 rows**.
- The existing image entrypoint, credentials Secrets, and blue-green strategy
  are retained. Source for the init hook: sclorg/mariadb-container 10.11
  `examples/extend-image/mysql-init/90-init-db.sh`.

## Selected classics

These are **20 representative works**, not an objective global ranking.
Bibliographic selections informed by Folha's 2022 expert poll, Brasil Escola's
20-classics list, and university Brazilian literature reading lists.

| ID | Work | Author | First publication / volume year |
|---:|---|---|---:|
| 1 | Memórias Póstumas de Brás Cubas | Machado de Assis | 1881 |
| 2 | Dom Casmurro | Machado de Assis | 1899 |
| 3 | O Cortiço | Aluísio Azevedo | 1890 |
| 4 | Iracema | José de Alencar | 1865 |
| 5 | O Guarani | José de Alencar | 1857 |
| 6 | Os Sertões | Euclides da Cunha | 1902 |
| 7 | Triste Fim de Policarpo Quaresma | Lima Barreto | 1915 |
| 8 | Macunaíma | Mário de Andrade | 1928 |
| 9 | Vidas Secas | Graciliano Ramos | 1938 |
| 10 | São Bernardo | Graciliano Ramos | 1934 |
| 11 | Capitães da Areia | Jorge Amado | 1937 |
| 12 | O Quinze | Rachel de Queiroz | 1930 |
| 13 | Grande Sertão: Veredas | João Guimarães Rosa | 1956 |
| 14 | Quarto de Despejo | Carolina Maria de Jesus | 1960 |
| 15 | A Hora da Estrela | Clarice Lispector | 1977 |
| 16 | A Paixão Segundo G.H. | Clarice Lispector | 1964 |
| 17 | O Continente (O Tempo e o Vento) | Erico Verissimo | 1949 |
| 18 | Morte e Vida Severina | João Cabral de Melo Neto | 1955 |
| 19 | Romanceiro da Inconfidência | Cecília Meireles | 1953 |
| 20 | A Moreninha | Joaquim Manuel de Macedo | 1844 |

`O Continente` is the first volume of the `O Tempo e o Vento` trilogy.

## Safe deployment sequence

**Do not commit/push the Rollout image change until the image exists at GHCR.**
The `full-multi` Argo CD Application auto-syncs from `main`; referencing a
missing image would create broken GREEN preview Pods.

From the repository root, after applying this patch locally:

```bash
bash -n scripts/build-push-mariadb-classics.sh
python3 scripts/full-multi-spec.py verify
oc kustomize full-multi >/dev/null
# GHCR_TOKEN is optional if your Podman/Docker login is already valid.
bash scripts/build-push-mariadb-classics.sh
# Ensure the package is publicly pullable or configure imagePullSecrets:
skopeo inspect --no-creds docker://ghcr.io/gstephan76/argo-sm-mariadb:classicos-brasileiros-20-v1

# Only after the image is pullable:
git add full-multi/mariadb-classics full-multi/rollout-mariadb-a.yaml \
  full-multi/rollout-mariadb-b.yaml full-multi/MARIADB-CLASSICS.md \
  scripts/build-push-mariadb-classics.sh
git diff --cached --check
git commit -m 'Seed MariaDB blue-green preview with 20 Brazilian classics'
git push origin main
```

The commit causes **both A and B** blue-green Rollouts to create a new GREEN
preview revision. `mariadb-a` is already paused with an earlier GREEN preview:
that preview may be replaced (and lose its `emptyDir` data), but its existing
BLUE active Pod remains active. B gets a GREEN preview for the first time if
it is currently Healthy with no candidate. Wait for readyness; inspect rollouts.

```bash
oc argo rollouts get rollout mariadb-a -n full-multi
oc argo rollouts get rollout mariadb-b -n full-multi

# Verify the preview via the existing Backend header selector.
HOST="$(oc -n full-multi get route full-multi -o jsonpath='{.spec.host}')"
for app in a b; do
  echo "=== $app / blue ==="
  curl -kfsS -H "x-app-target: $app" -H 'x-front-track: stable' \
    -H 'x-backend-track: stable' -H 'x-db-track: stable' \
    "https://${HOST}/api/books" | jq '.database_host, (.books | length)'
  echo "=== $app / green ==="
  curl -kfsS -H "x-app-target: $app" -H 'x-front-track: stable' \
    -H 'x-backend-track: stable' -H 'x-db-track: canary' \
    "https://${HOST}/api/books" | jq '.database_host, (.books | length), .books'
done
```

Expected for a fresh GREEN instance: 20 books, correct accented Portuguese
names, and the `mariadb-{a,b}-canary` database host. BLUE should still have
its original demo books (typically 3). Check errors if a candidate Pod is not
ready or Argo CD is out of sync.

**No automatic promotion.** `autoPromotionEnabled: false` remains configured.
The `emptyDir` preview is disposable: this demonstrates blue-green data
separation, *not* database replication or safe production migration.

**Separate follow-up:** the requested MariaDB HTTP-header VirtualService +
HTTP-to-MySQL adapter is **not** implemented by this image change. The current
`x-db-track` selector continues to work in the Go Backend; direct MySQL
connections cannot carry HTTP headers.
