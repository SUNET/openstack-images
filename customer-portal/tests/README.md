# customer-portal tests

Two layers of testing live in this directory:

1. **Local automated suite** — fast, deterministic, no infra except a
   throwaway Postgres. Covers pure logic, the synthetic-billing
   emitter (DB-backed), the OpenBao HTTP client (HTTP-mocked), and the
   FastAPI router stack (real DB + httpx ASGITransport, with the
   tenant-cluster boundary mocked), local bare-Git publication and browser
   interactions with the actual SPA and mocked remote APIs.
2. **Live end-to-end walkthrough** — runs against a real kubespray
   cluster + OpenBao mount + the deployed portal. Recipe is in the
   in-portal setup guide (`Admin → Clusters → Setup guide`).

## Running the local suite

### In code-box

Code-box includes PostgreSQL server and client tools. Run the complete suite
in a throwaway cluster with:

```bash
pg_virtualenv bash -c '
    createdb portal_test &&
    env PORTAL_TEST_DB_URL=postgresql+asyncpg:///portal_test \
        pytest
'
```

`pg_virtualenv` creates the cluster before the command and removes it
afterward. The hostless database URL uses the temporary Unix socket and port
provided by `pg_virtualenv`.

### With Docker

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

Allow several minutes for the complete suite, including browser and real Git
workflows. The conftest fixture runs
`alembic upgrade head` once per session against the test DB; subsequent
test runs reuse the schema and TRUNCATE non-seed tables before each
DB-backed test.

### GitOps prerequisites and verification

Install the development extras and a Playwright Chromium browser before the
browser run. In a normal development image:

```bash
.venv/bin/python -m playwright install chromium
.venv/bin/python -m pytest tests/test_gitops_browser.py
```

Chromium's Linux runtime libraries must be available in the development image;
the production portal image does not include browser tooling. The publisher
tests require Git and standalone Kustomize on `PATH`. PostgreSQL server tools
(`pg_config`, `initdb`, `pg_ctl`) are required for lifecycle/migration tests,
which start private Unix-socket-only servers and clean them up automatically.
Other DB-backed suites use the disposable database configured above. Never
point these fixtures at a live portal database: tests migrate/truncate tables.

The important new suites are:

- `test_api_customer_repositories.py`: shared binding ownership, isolation,
  rotation/CAS recovery, validation and redaction with real PostgreSQL.
- `test_gitops_lifecycle.py`: actual API/worker transactions, explicit approval,
  multi-replica locks, cancellation, stale inputs, and confirmed Git publication
  followed by DB commit failure and recovery.
- `test_gitops_source.py`: exact inventory provenance, authoritative values,
  readiness and categorized Kubernetes failures.
- `test_gitops_publisher.py`, `test_gitops_recovery.py`: real disposable Git
  repositories, initial publication, second cluster, adoption, safe updates,
  rejected/racing pushes, no-op retries and read-only recovery.
- `test_gitops_render.py`, `test_gitops_reviewed.py`: schema, Kustomize, pruning
  protections and parity with the reviewed object shapes.
- `test_gitops_browser.py`: interactive create/edit, credential preservation,
  CAS recovery, live clusters, preview approval and status polling.
- `test_migration_015.py`: a populated 014 database is upgraded without
  changing tenant, access, credential-issuance or accounting history.
- `test_release.py`: runtime, Jenkins and deployment version consistency.

No tests use real Forgejo credentials, push to a remote customer repository,
or apply objects to a live Kubernetes cluster. A passing mocked API test does
not replace the documented operator acceptance checks after deployment.

### Existing coverage

| File | Surface |
|---|---|
| `test_unit.py` | Size labels, CSR/kubeconfig builders, validation, issuance status. |
| `test_billing_runner.py` | Metric-family-aware billing queries, usage history, export delivery. |
| `test_migration_009.py`, `test_migration_010.py` | Pricing migration convergence and preservation. |
| `test_billing_synthetic.py` | Provisioning, resize and addon accounting periods. |
| `test_openbao_client.py` | Kubernetes login/mint, KV v2 CAS/versioning, expiry and error handling. |
| `test_api_clusters.py` | Creation reuse, versioned editing, live connection guards, access/issuance concurrency and lifecycle boundaries. |

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

The canonical live procedure is the access-restricted **Customer Kubernetes
clusters** runbook at
<https://docs.sunetdc.se/customer-kubernetes/>. The in-portal setup guide is a
summary and must not replace its readiness gates.

The runbook uses the managed `portal-access` base and the reviewed OpenBao
helpers. Do not recreate their service accounts, RBAC, or secrets from an old
inline walkthrough. A live test must prove that an issued kubeconfig can list
Argo CD Applications in `argocd` and receives a denial outside that namespace.

OpenBao requests 600-second manager tokens, but Kubernetes RBAC cannot cap the
TokenRequest duration for a holder of the long-lived minter token. Treat that
token as a privileged cluster credential rather than a hard short-lived-token
boundary.

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
