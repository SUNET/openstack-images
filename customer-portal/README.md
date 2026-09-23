# Customer portal

## Managed cluster configuration

The portal writes one `ManagedCluster` manifest per planned cluster to the
configured cluster Git repository. The root `kustomization.yaml` is maintained
in the same commit and contains only sorted `clusters/*/cluster.yaml` entries;
generated inventory files are intentionally excluded.

`CLUSTER_PROFILE_NAME` selects `spec.profileRef.name` in these manifests and
defaults to `standard-v1`. Other values are rejected at startup because the
managed project quota formula is currently specific to that profile.

New manifests start active with explicit `spec.suspend: false` and use
`spec.deletionPolicy: Retain`. Portal deletion of any published managed cluster
is disabled in phase one, even if the manifest is suspended. Cluster, project,
and generated state requires coordinated manual decommissioning.

Managed OpenStack project quotas are sized from the requested worker-group
count. The standard profile has three `b2.c2r4` controllers, three `b2.c4r16`
workers per group, and one `b2.c1r2` jump host. Each Kubernetes node receives a
100 GB boot volume and the jump host receives 20 GB. Existing snapshot and
security defaults are retained; managed projects also receive at least three
floating IPs and two ports per instance.

Cluster creation and resize requests allow 1 to 80 worker groups. The upper
bound leaves address headroom for the `standard-v1` profile in its `/24`
network. The managed project quota formula continues to scale from the selected
worker-group count.

`CLUSTER_PROVISIONER_USER` names the service user that provisions resources in
managed OpenStack projects and defaults to `openstack-operator`.
`CLUSTER_PROVISIONER_USER_DOMAIN` names that user's Keystone domain and
defaults to `default`. Every managed project keeps this identity in a `member`
role binding. All `PORTAL_ADMIN_USERS` are also maintained as `member` users in
the portal's default user domain, while per-cluster customer admins are
maintained separately as `reader` users there. Managed direct assignments are
currently add-only in the OpenStack operator to avoid revoking manual or
cross-domain assignments, so removing either kind of admin from portal state
requires a manual Keystone revocation. Self-service projects retain their
existing single customer `member` binding.

## Customer GitOps lifecycle

Repository configuration belongs to a customer and an explicit environment,
not to an individual cluster creation request. SUNET administrators configure
and validate it on customer or cluster detail pages. Creating a cluster reuses
the shared configuration without reading or changing its credentials.

- Repository URLs/usernames and credential version references are database
  metadata. Tokens are written only to versioned OpenBao KV paths.
- Explicit credential replacement uses CAS. Blank UI fields preserve existing
  credentials. Reader installation remains manual, with per-cluster version
  acknowledgement before revoking an old shared token.
- Existing and active clusters can attach GitOps without recreating their
  infrastructure or resetting provisioning, access, or accounting history.
- Cluster metadata edits require `config_version`. Display-name editing is a
  portal label; an Argo CD alias is requested metadata, not DNS/TLS activation.
  Active issued credentials block connection changes requiring a migration.

The cluster GitOps page queues a preview, displays its source snapshot,
validated manifests and effective bases revision, and requires explicit
publication approval. The PostgreSQL-backed worker resumes queued/running
operations across restarts and serializes work per shared repository. A
confirmed remote commit is recorded separately from service activation.
Recovery checks for an already completed push before imposing current-input
freshness requirements; a new push rechecks infrastructure immediately before
publication. Errors never contain transport output or credential payloads.

Generated inventory is read from `ManagedCluster.status.inventoryCommit`, not
the mutable branch tip. The canonical hostname, allocated VIP and reviewed
interface cannot be overridden by coherent but conflicting manual Git edits.
Compatible manual ACME-contact edits are preserved. Existing root documents
and gitlinks are retained when adding a second cluster. Adoption is explicit;
overlapping manual edits require a new review rather than force-pushing.

Required deployment settings for this feature:

| Setting | Purpose |
| --- | --- |
| `CLUSTER_ENVIRONMENT` | Explicit `test` or `prod`; never inferred from the UI colour |
| `MANAGED_CLUSTER_NAMESPACE` | Namespace containing the infrastructure declarations |
| `CUSTOMER_REPOSITORY_ORIGIN` | Allowed HTTPS Forgejo origin |
| `CUSTOMER_CLUSTER_BASES_REVISION` | Approved immutable default for a new repository |
| `CUSTOMER_CLUSTER_BASES_URL` | Reviewed public bases source |
| `CUSTOMER_CLUSTER_NODE_INTERFACE` | Reviewed Ansible interface policy, default `ens3` |
| `CUSTOMER_CLUSTER_ACME_CONTACT` | Default for a new cluster draft |
| `GITOPS_WORKER_ENABLED` | `1` enables execution; `0` blocks queueing new operations |

The image includes standalone Kustomize; missing rendering prerequisites fail
closed. Migration `015` preserves existing records and adds versioned settings
and operation history without contacting Git, Kubernetes or OpenBao. Configure
unbound legacy clusters explicitly; do not infer customer URLs from slugs.

The canonical operational runbook is maintained in platform-manifests under
`docs-site-internal/content/docs/customer-kubernetes/`. Base upgrades, repository
relocation, infrastructure resize and decommissioning remain reviewed manual
workflows. This feature does not add operator infrastructure-mutation support.

## Repository credential validation (0.1.25)

Create the portal writer as a Forgejo **Specific repositories**
(`SpecificRepositories`) token, selecting only the private customer repository
and granting `write:repository`. Use a separate `read:repository` token for
tenant Argo CD. No `read:user`, account, global-repository, or administrator
scope is necessary.

The **Validate** action uses only `GET /api/v1/repos/{owner}/{repo}` for Forgejo
API metadata. It requires the returned repository to match the configured
HTTPS URL and owner/name identity, be private, and report `permissions.push`
as true. It then runs read-only `git ls-remote` against that repository using
the supplied username/token through isolated HTTPS authentication to check Git
read access. The username/token are checked as Git credentials.

Validation never creates a commit, pushes, or mutates the remote repository.
Repository metadata alone cannot prove a narrowly scoped token's actual write
permission; real publishing checks push authorization and protected-branch
restrictions.

Release 0.1.24 could reject a correctly scoped writer because it queried
`/api/v1/user`, which can return `403` for a specific-repository token. Release
0.1.25 removes that account-read dependency. Reuse the existing stored token:
after building/releasing 0.1.25 and syncing the portal Application, click
**Validate** again in the shared customer repository editor. Upgrading from
0.1.24 requires no token rotation, database or OpenBao migration, or sync of
other Applications.

## Releasing

The package version in `pyproject.toml` is the application version source. The
running application uses installed distribution metadata, with the project
metadata as its source-checkout fallback. Set a new package version and
matching immutable Jenkins image tag together from the repository root:

```console
python3 customer-portal/scripts/set_version.py 0.1.27
```

The release tests reject a mismatched package and image-tag version. Updating
the deployment manifest remains a separate, deliberate image-promotion step.
