# customer-portal tests

Two layers of testing live in this directory:

1. **Local automated suite** — fast, deterministic, no infra except a
   throwaway Postgres in Docker. Covers pure logic, the synthetic-billing
   emitter (DB-backed), the OpenBao HTTP client (HTTP-mocked), and the
   FastAPI router stack (real DB + httpx ASGITransport, with the
   tenant-cluster boundary mocked).
2. **Live end-to-end walkthrough** — runs against a real kubespray
   cluster + OpenBao mount + the deployed portal. Recipe is in the
   in-portal setup guide (`Admin → Clusters → Setup guide`).

## Running the local suite

### Prereqs

A Postgres 16 reachable at `localhost:55432` with database `portal_test`,
user `portal`, password `portal`. Easiest way:

```bash
docker run -d --rm --name portal-test-pg \
    -e POSTGRES_USER=portal \
    -e POSTGRES_PASSWORD=portal \
    -e POSTGRES_DB=portal_test \
    -p 55432:5432 \
    postgres:16
```

To point the suite at a different DB, set `PORTAL_TEST_DB_URL`
(must use the `+asyncpg` driver):

```bash
export PORTAL_TEST_DB_URL=postgresql+asyncpg://user:pass@host:port/db
```

Install dev deps once:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

(or just install the runtime deps from `pyproject.toml` plus
`pytest`, `pytest-asyncio`, `respx` if `pip install -e .` complains
about the flat layout).

### Run

```bash
pytest                     # all tests
pytest tests/test_unit.py  # one file
pytest -k billing          # filter by name
pytest -x                  # stop on first failure
```

Expect ~2s wall time for the full suite. The conftest fixture runs
`alembic upgrade head` once per session against the test DB; subsequent
test runs reuse the schema and TRUNCATE non-seed tables before each
DB-backed test.

### What's covered

| File | Surface | Count |
|---|---|---|
| `test_unit.py` | Pure-logic helpers: size labels, CSR/kubeconfig builders, OIDC-sub hashing, payload + slug-regex validation, issuance status. | 24 |
| `test_billing_runner.py` | Fail-closed, metric-family-aware Gnocchi queries and pagination, history-aware usage aggregation, logical storage GB-month pricing, canonical Cinder volume-type rollup, contract mapping, and empty combined-report delivery protection. | 31 |
| `test_migration_009.py` | Cinder volume-price migration convergence, live-price preservation, and downgrade behavior. | 4 |
| `test_migration_010.py` | Snapshot and backup base-price insertion, custom-price preservation, duplicate convergence, idempotency, and downgrade behavior. | 5 |
| `test_billing_synthetic.py` | DB-backed `_emit_synthetic_cluster_lines`: provisioning period, subsequent period, applied resize, per-contract override, addon disable boundary, unprovisioned cluster. | 6 |
| `test_openbao_client.py` | HTTP shape contract with OpenBao: K8s-auth login body, creds POST (regression-proofs the GET→POST fix), 403 retry, error propagation. | 4 |
| `test_api_clusters.py` | FastAPI router stack with tenant-cluster boundary mocked: cluster create/provision, access mgmt + RBAC negatives, kubeconfig issue + cascade-revoke, addon/resize/backup request flows, managed-project policy gate. | 11 |

Total: 85 tests.

### What's deliberately mocked

The tests stop at the **portal's outbound boundary**:

- `app.kubeconfig_service.{issue,revoke,cascade_revoke_for_user}` are
  monkey-patched in `tests/test_api_clusters.py`, so no real OpenBao or
  tenant K8s API calls are made. The mocks still touch the real DB so
  issuance metadata persistence is exercised.
- `app.git_backend.GitBackend` is replaced with `StubGitBackend` (an
  in-memory dict); no real git push happens.
- `app.openbao_client` HTTP calls are intercepted by `respx` in its
  unit tests.

What's **real**:

- Postgres, alembic migration, all SQLAlchemy queries.
- The full FastAPI router stack with auth dependencies overridden.
- Pydantic validation, schema serialisation.
- All sync/async-context boundary handling.

## Live end-to-end walkthrough

For a fresh tenant cluster, follow the validated recipe in the in-portal
**Setup guide** (Admin → Clusters → Setup guide). Every step there has
been exercised live against the `sunet-test` cluster on 2026-04-30; the
guide includes inline notes for the gotchas we hit:

- OpenBao policy needs `update`, not `read`, for credential mint
  (kubernetes secrets engine generates via POST).
- OpenBao role's `allowed_kubernetes_namespaces` should be a single
  entry (`kube-system`), the namespace where `openbao-rbac-manager`
  lives.
- Token TTL must be ≥ 600s (K8s TokenRequest API minimum).
- The `argocd-rbac-manager` ClusterRole needs:
    - `create` on `serviceaccounts/token` for the bound SA, so OpenBao
      can mint TokenRequest tokens for itself.
    - `bind` on `roles` (resourceNames `argocd-tenant`) so K8s privilege-
      escalation prevention lets it create RoleBindings to that Role.
- OIDC subs are stored as **annotations**, not labels, on RoleBindings —
  K8s label values can't contain `@` or `/`. The label is a sha256 hash
  of the sub for filterable cascade-revoke; the annotation carries the
  raw sub for human readability.

Verification once the live flow lands:

```bash
# Check the full stack
kubectl --context <tenant> get csr | tail -10
kubectl --context <tenant> get rolebindings -n argocd \
  -o jsonpath='{range .items[?(@.metadata.labels.sunet\.se/oidc-sub-hash)]}{.metadata.name}{"  hash="}{.metadata.labels.sunet\.se/oidc-sub-hash}{"  sub="}{.metadata.annotations.sunet\.se/oidc-sub}{"\n"}{end}'

# Use the issued kubeconfig
KUBECONFIG=./issued.yaml kubectl get applications -n argocd      # should succeed
KUBECONFIG=./issued.yaml kubectl get pods -n kube-system         # should be 403
```

## Adding new tests

- Pure logic? Add to `test_unit.py`.
- Touches `tenant_cluster`/`cluster_*`/billing tables? Add to
  `test_billing_synthetic.py` if it's billing math, or
  `test_api_clusters.py` if it goes through an HTTP endpoint.
- Hits OpenBao? Add to `test_openbao_client.py` with `respx.mock`.
- New router? New file, follow the `client` fixture pattern from
  `test_api_clusters.py` (uses `httpx.AsyncClient` + `ASGITransport` —
  do **not** switch to `fastapi.testclient.TestClient`, it spins up a
  separate AnyIO loop and breaks the async DB session fixture).
