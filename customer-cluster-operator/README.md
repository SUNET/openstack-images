# Customer Cluster Operator

Current package and image release: `0.1.6`.

This directory contains the first infrastructure slice of the
`ManagedCluster` operator. A Kopf controller validates each desired cluster and
runs an idempotent worker Job. The worker provisions or verifies OpenStack
infrastructure and atomically publishes host and cluster policy inventories in
one Git commit. Kubernetes installation remains a manual Ansible/Kubespray step.

## Namespace model

`ManagedCluster` resources, provisioning Jobs, input ConfigMaps, referenced
Secrets, and referenced ConfigMaps must all be in `OPERATOR_NAMESPACE`, which
defaults to `openstack-operator`. `ClusterProfile` remains cluster-scoped.
Secret and ConfigMap cross-namespace references are rejected. The controller
checks that each referenced object and key exists, but never logs or decodes its
value. The profile's OpenstackProject may
be in a separate `projectNamespace`, such as `customer-projects`.

The controller expects the referenced `OpenstackProject` in the profile's
`projectNamespace` to use `sunet.se/v1alpha1`. It requires `status.phase:
Ready`, a non-empty `status.projectId`, and `status.observedGeneration` exactly
matching `metadata.generation`, as well as matching project and contract names
and `spec.managed: true`.

## Configuration

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `WORKER_IMAGE` | yes | none | Image used by provisioning Jobs |
| `OPERATOR_NAMESPACE` | no | `openstack-operator` | Only accepted namespace |
| `WORKER_SERVICE_ACCOUNT` | no | `customer-cluster-worker` | Distinct Job service account |
| `VERIFICATION_INTERVAL_SECONDS` | no | `900` | Ready-state verification interval |

The worker image needs no Kubernetes API token. Its Job receives:

- `clouds.yaml` from `profile.spec.openstack.credentialsSecret`;
- public keys from `profile.spec.ssh.authorizedKeysConfigMap`;
- the Git token through a `secretKeyRef`; and
- immutable, canonical provisioning input from an owner-referenced ConfigMap.

The credentials Secret's configured key may have any name; it is projected as
`/etc/openstack/clouds.yaml`. Public-key ConfigMaps must contain only OpenSSH
public keys. Private keys are rejected.

### Explicit inventory policy inputs

Both reviewed `standard-v1` deployment overlays (`test` and `prod`) set:

```yaml
spec:
  ansible:
    nodeInterface: ens3
    pythonInterpreter: /usr/bin/python3
```

These are explicit reviewed values, not defaults. `nodeInterface` must match
`[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}`: a literal Linux interface name, at most 15
characters. `pythonInterpreter` must be a normalized absolute path of at most
512 characters, using only `/A-Za-z0-9_./+-`, with basename `python3` or
`python3.NUM`. Empty, `.` or `..` path segments, a trailing slash, whitespace,
and shell metacharacters are rejected. For example, `/usr/bin/python3.13` is
valid; `/usr/../bin/python3` and `/usr/bin/python3 -I` are not.

New Jobs also require explicit `ManagedCluster.spec.dns.apiHostname` and
`argocdHostname`: lowercase ASCII FQDNs of at most 253 characters, without a
trailing dot or an IP literal. Neither hostname is derived from a slug, zone,
customer domain, or `argocdAlias`.

The CRDs keep top-level `spec.ansible` on ClusterProfile and `spec.dns` on
ManagedCluster optional for older objects and API writes during rollout. When
`ansible` is present, both its fields are required. Canonical DNS fields remain
admission-optional. The 0.1.6 runtime requires all four explicit values before
creating a Job; schema permissiveness does not supply runtime defaults.

The source ClusterProfile must also exist, be non-deleting, and have a
non-empty string `metadata.uid` and a positive integer `metadata.generation`.
Kubernetes supplies this metadata; it is not set in the reviewed overlay.
Missing or invalid source-profile metadata, or a deleting profile, prevents
new Jobs from being created.

## Reconciliation and retention

The full reconcile for each ManagedCluster UID is serialized by an in-process
lock shared by Kopf timer and change-handler executor threads. The lock is
acquired before reading the source ClusterProfile and deciding which Job to
create or accept. It is not distributed locking or leader election. The
supported controller Deployment requires exactly **one replica** and the
**`Recreate`** strategy; do not scale operator replicas or run a second
controller for the same managed resources.

The controller keeps three distinct SHA-256 identities:

| Status field | Contract |
| --- | --- |
| `inputHash` | Exact v1 immutable infrastructure hash; excludes inventory policy and profile revision |
| `inventoryInputHash` | Resolved allowlisted render fields only; excludes profile revision |
| `publicationHash` | Full immutable worker Job input, including render policy version 2 and profile revision |

`build_input` requires an explicit `profile_revision` mapping with the source
ClusterProfile's `uid` and positive integer `generation`. It stores that mapping
as root `profileRevision` in the internal worker payload. This is not a CRD
spec field. The preserved v1 infrastructure hash excludes it, while the full
publication hash includes it.

The publication hash identifies the Job and its input ConfigMap. Their
deterministic identity includes a freshness nonce incorporating the source
ClusterProfile UID and generation, alongside cluster UID, ManagedCluster
generation, and a verification time bucket. A profile edit from A to B and
back to A therefore cannot reuse A's completed receipt, even in the same
ManagedCluster generation and time bucket. The resolved `inventoryInputHash`
can return to A's value, but its `publicationHash` is new. The default 15-minute
bucket launches at most one periodic verification Job per unchanged input and
profile revision per interval, rather than one per 30-second timer tick. Full
hashes are retained in annotations and status; labels use a Kubernetes-safe
63-character prefix.

Once provisioning has started, changed infrastructure-affecting inputs are
marked `Failed` with `InfrastructureDriftUnsupported`. Inventory policy and
canonical DNS changes do not alter the v1 infrastructure hash or trigger that
error. A profile-only policy change gets a new Job and ConfigMap even when the
ManagedCluster generation and verification bucket are unchanged. Display and
OpenBao metadata remain outside the infrastructure hash; generation changes
also launch verification Jobs. Every new Job still idempotently provisions or
verifies retained OpenStack resources before publishing inventory. Metadata
changes neither recreate nor resize VMs and never run Ansible.

`spec.dns.argocdAlias` is optional metadata containing a lowercase ASCII FQDN.
It does not affect provisioning, either inventory, the input hash,
infrastructure drift, Gateway resources, DNS records, or certificates.

Before launching replacement work or accepting a Ready receipt, the controller
waits for **all unfinished owned Jobs** for the cluster to become terminal.
This includes pending Jobs and retry/backoff Jobs with `status.active: 0`;
having no active Pods does not make a Job finished. Terminal completion requires
a `Complete` or `Failed` condition with status `True` and no active or
terminating Pods. Success/failure counters alone are insufficient. A completed
older receipt cannot bypass this wait or satisfy a newer profile revision.

Per cluster, history cleanup retains at most two completed verification Jobs,
plus all unfinished Jobs, the current Job, and the Job holding the current
status result. Deleting an old Job uses background propagation so Kubernetes
garbage-collects its Pods. Matching owner-validated input ConfigMaps and orphaned
historical input ConfigMaps are removed explicitly. Unfinished/current Jobs
and their result objects are never removed.

`spec.suspend` defaults to `false`, so an absent field is active and
provisioning starts automatically after the referenced OpenstackProject is
validated as Ready. Set it to `true` as an emergency override; a suspended
cluster remains in `Suspended` without creating a Job.

Deletion is always retain-only. The operator never deletes OpenStack resources
or either inventory file. Kubernetes garbage collection may remove the
owner-referenced Job and input ConfigMap when the `ManagedCluster` is deleted.

Status phases are `Suspended`, `PendingProject`, `PendingPrerequisites`,
`ProvisioningInfrastructure`, `VirtualMachinesReady`, and `Failed`. Ready status
is derived from the current owned Job Pod's structured termination receipt.
Its metadata, paths, infrastructure hash, inventory-input hash, and publication
hash must match the current desired Job and source profile revision before
reporting Ready. Status records `inventoryPath`, `policyInventoryPath`,
`inventoryCommit`, `apiFloatingIp`, and `ingressFloatingIp`; successful checks
also update `lastVerifiedAt`.
`inventoryCommit` covers both inventory files. A completed stale Job or a 0.1.5
single-file receipt cannot establish the v2 pair as Ready. Readiness means
OpenStack resources exist and the inventory pair is published or verified,
not that Kubernetes was installed.

## OpenStack behavior

The worker scopes the selected `clouds.yaml` cloud to the exact project ID and
name, verifies the resulting scope, and then creates deterministic resources:

- private network, subnet, SNAT router, and required reserved API and ingress VIP ports;
- shared cluster, jumphost, API endpoint, and ingress endpoint security groups;
- stable floating IPs attached directly to the jumphost and both VIP ports;
- three controllers and `3 * workerGroups` workers; and
- boot-from-volume Debian Trixie instances with password SSH disabled.

`workerGroups` may not exceed the profile's required `maxWorkerGroups`. The
profile maximum must fit the network after accounting for three controllers,
the jumphost, two VIPs, router, DHCP, and at least four spare addresses.

Only configured CIDRs may reach jumphost TCP/22. Cluster-node ingress permits
internal cluster-security-group traffic and TCP/22 from the jumphost security
group. Controllers additionally receive an API group allowing public TCP/6443;
workers receive an ingress group allowing public TCP/80 and TCP/443. Existing
owned node ports are reconciled from the former shared or `sunet-two` group set
to these exact role-specific sets without replacing ports or servers. Node SSH
is not exposed publicly. Existing same-name resources not
marked with this cluster UID cause a fail-closed ownership error.
Retained same-name resources are never adopted across cluster UIDs. Recovery
requires restoring the original ManagedCluster UID, selecting a different
cluster slug, or manually resolving the retained resource after review.

## Inventory publication

Git access requires an HTTPS URL without embedded credentials. HTTP Basic auth
is supplied through process environment Git configuration, so the token is not
placed in the URL, command line, generated inventory, or repository config.
Push races retry from fresh clones and recheck policy ownership. The API-less
worker publishes both paths atomically in **one commit** in the management
environment repository:

```text
clusters/<slug>/generated/ansible/hosts.yml
inventory/clusters/<slug>.yml
```

Host inventory `all.vars` publishes the private API and ingress VIPs and both
public endpoint floating IPs. `kube_node` contains workers only; `k8s_cluster`
includes both `kube_control_plane` and `kube_node` through child groups.

Cluster policy inventory contains only these derived, non-secret `all.vars`:

| Variable | Source |
| --- | --- |
| `customer_cluster_name` | ManagedCluster name/slug |
| `customer_cluster_profile` | Selected `spec.profileRef.name` |
| `customer_cluster_node_interface` | ClusterProfile `spec.ansible.nodeInterface` |
| `customer_cluster_api_hostname` | ManagedCluster `spec.dns.apiHostname` |
| `customer_cluster_argocd_hostname` | ManagedCluster `spec.dns.argocdHostname` |
| `ansible_python_interpreter` | ClusterProfile `spec.ansible.pythonInterpreter` |

This release publishes the profile's node interface only as Ansible inventory
policy. The portal still uses its separately reviewed
`CUSTOMER_CLUSTER_NODE_INTERFACE` setting when rendering tenant Cilium ingress
policy. Both currently use `ens3`; review and keep the portal setting and
tenant Cilium L2 policy aligned with the Ansible value. A profile change does
not automatically propagate to portal configuration or installed tenant ingress.

Policy ownership is fail-closed:

- If absent, the worker creates policy with a generated ownership marker for
  the ManagedCluster UID.
- Policy owned by that same UID can be updated from the current reviewed inputs.
- An existing unmarked manual file in the Git branch with exactly the derived
  allowlisted values is accepted byte-for-byte, including its comments and
  formatting. It remains unmarked and manually owned; acceptance is not adoption.
- Differing manual values, unknown extra fields, or another UID's marker are
  conflicts. The worker never automatically adopts or overwrites those files.

If neither file changes, verification returns the existing commit without an
empty commit. Shared `inventory/environments/<environment>.yml` and
`ansible/inventory/profiles/<profile>.yml` remain manually reviewed. The four
Ansible inventories retain their order: generated hosts, shared profile,
environment policy, then cluster policy. Verify both cluster files at the
recorded `status.inventoryCommit` before the manual installation.

## Upgrade from 0.1.5 to 0.1.6

1. Make the reviewed operator source and canonical CRDs available and build
   `docker.sunet.se/platform/customer-cluster-operator:0.1.6`. The deployment
   base fetches CRDs from GitHub `main`, so that source must contain these CRD
   changes before its GitOps sync; a local render of older remote CRDs is not
   evidence of the new schema.
2. Deploy the updated CRDs, then the reviewed ClusterProfiles with explicit
   Ansible values in both environments, then the controller image and
   `WORKER_IMAGE` at `0.1.6`. Confirm existing ManagedClusters have both canonical
   DNS hostnames before expecting new Jobs. The live source ClusterProfile must
   have server-supplied UID/generation metadata and must not be deleting.
3. Preserve each ManagedCluster UID and `status.inputHash`. Do not clear the
   immutable hash or delete/recreate the CR as an upgrade mechanism. Wait for
   all unfinished Jobs, including pending/backoff Jobs with no active Pods.
   Unfinished 0.1.5 Jobs may finish, but their single-file receipts cannot report
   v2 readiness.
4. The next eligible reconciliation runs a v2 worker against retained
   infrastructure and backfills an absent policy file. Inspect
   `status.policyInventoryPath`, `inventoryInputHash`, `publicationHash`, and
   `inventoryCommit`, and verify both paths at that commit before continuing.

An existing handwritten `inventory/clusters/eosc-one.yml` may remain untouched
and manually owned when committed to the Git branch and its values match
exactly. Do not delete it to force generation. Resolve differing manual policy
through a separately reviewed change; shared environment/profile policy still
needs its own review. This release does not itself execute Ansible or change
the installed Kubernetes configuration.

The worker publishes only to the Git branch; it cannot see or overwrite an
untracked file in a sysop's local checkout. An untracked policy at the same path
as a newly published file, including a locally prepared `eosc-one.yml`, can
cause a subsequent pull to refuse the collision. Preserve the local contents
outside the checkout or move the file aside before pulling, then compare it
with the published policy. If the branch does not yet contain that path, an
alternative is to deliberately review and commit/publish the exactly matching
manual policy before operator publication. Do not discard local work or assume
that local presence establishes policy ownership in the remote branch.

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest
python -m compileall -q src tests
```

Canonical `ManagedCluster` and `ClusterProfile` CRDs are stored under `crds/`.
Deployment manifests consume these files from the repository; controller
Deployment, service account, and RBAC manifests remain deployment-owned.

`tests/test_crds.py` checks structural constraints and the string pattern/CEL
subset offline; `tests/test_release.py` checks package/build pins and, when the
sibling checkout exists, both deployment images. The platform checkout's
`customer-cluster-operator/tests/test_deployment.py` renders both overlays with
these local CRDs substituted only in scratch copies, refuses remote inputs,
and validates rendered profiles with this checkout's runtime model. It passes
synthetic `profile_revision={"uid": "profile-uid", "generation": 1}` explicitly;
rendered manifests do not contain the live server-supplied revision. This
offline render does not inspect the deployed API schema or GitHub `main`.

Deployment manifests need one optional environment variable when exposing the
new setting: `VERIFICATION_INTERVAL_SECONDS` (default `900`, minimum `60`). A
structural status schema must permit `status.lastVerifiedAt` as a date-time
string and both endpoint floating-IP fields as IPv4 strings. Controller RBAC
must grant `get`, `list`, `create`, and `delete` on
`batch/jobs`; `get`, `list`, `create`, and `delete` on core `configmaps`; `get`
on core `secrets`; `list` on core `pods`; and cluster-scoped `get`, `list`, and
`watch` on core `namespaces` for Kopf discovery. Pod deletion is not required
because Job owner garbage collection removes historical Pods.
