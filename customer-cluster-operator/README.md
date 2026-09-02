# Customer Cluster Operator

This directory contains the first infrastructure slice of the
`ManagedCluster` operator. A Kopf controller validates each desired cluster and
runs an idempotent worker Job. The worker provisions OpenStack infrastructure
and commits generated Kubespray inventory. It deliberately does **not** run
Ansible or Kubespray; an operator performs that step manually later.

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

## Reconciliation and retention

The controller uses a canonical SHA-256 hash over infrastructure-affecting
inputs and a deterministic Job name derived from cluster UID, hash, CR
generation, and a verification time bucket. The default 15-minute bucket
launches at most one periodic idempotent verification/reconciliation Job per
interval, rather than one per 30-second timer tick. Unchanged inventory is not
committed. The full hash is retained in annotations and status; labels use a
Kubernetes-safe 63-character prefix. Once a Job has been created, changed
infrastructure inputs or profile data are marked `Failed` with
`InfrastructureDriftUnsupported`. Display, DNS, and OpenBao fields do not alter
the infrastructure hash. Their generation changes launch an idempotent worker
Job that verifies and reconciles retained resources before Ready is reported.

Per cluster, history cleanup retains at most two completed verification Jobs,
plus the active/current Job and the Job holding the current status result.
Deleting an old Job uses background propagation so Kubernetes garbage-collects
its Pods. Matching owner-validated input ConfigMaps and orphaned historical
input ConfigMaps are removed explicitly. Active/current Jobs and their result
objects are never removed.

`spec.suspend` defaults to `false`, so an absent field is active and
provisioning starts automatically after the referenced OpenstackProject is
validated as Ready. Set it to `true` as an emergency override; a suspended
cluster remains in `Suspended` without creating a Job.

Deletion is always retain-only. The operator never deletes OpenStack resources
or generated inventory. Kubernetes garbage collection may remove the
owner-referenced Job and input ConfigMap when the `ManagedCluster` is deleted.

Status phases are `Suspended`, `PendingProject`, `PendingPrerequisites`,
`ProvisioningInfrastructure`, `VirtualMachinesReady`, and `Failed`. Ready status
is derived from an owned
Job Pod's structured termination result and records `inventoryPath` and
`inventoryCommit`. Successful checks also update `lastVerifiedAt`. It means
OpenStack resources exist and inventory was pushed, not that Kubernetes was
installed.

## OpenStack behavior

The worker scopes the selected `clouds.yaml` cloud to the exact project ID and
name, verifies the resulting scope, and then creates deterministic resources:

- private network, subnet, SNAT router, and optional reserved VIP ports;
- separate cluster and jumphost security groups;
- one jumphost with a floating IP;
- three controllers and `3 * workerGroups` workers; and
- boot-from-volume Debian Trixie instances with password SSH disabled.

`workerGroups` may not exceed the profile's required `maxWorkerGroups`. The
profile maximum must fit the network after accounting for three controllers,
the jumphost, two VIPs, router, DHCP, and at least four spare addresses.

Only configured CIDRs may reach jumphost TCP/22. Cluster-node ingress permits
internal cluster-security-group traffic and TCP/22 from the jumphost security
group. Node SSH is not exposed publicly. Existing same-name resources not
marked with this cluster UID cause a fail-closed ownership error.
Retained same-name resources are never adopted across cluster UIDs. Recovery
requires restoring the original ManagedCluster UID, selecting a different
cluster slug, or manually resolving the retained resource after review.

## Inventory publication

Git access requires an HTTPS URL without embedded credentials. HTTP Basic auth
is supplied through process environment Git configuration, so the token is not
placed in the URL, command line, generated inventory, or repository config.
Push races retry from fresh clones. Inventory is written to:

```text
clusters/<slug>/generated/ansible/hosts.yml
```

## Development

```bash
python -m pip install -e '.[dev]'
ruff check .
ruff format --check .
pytest
python -m compileall -q src tests
```

No CRDs or deployment manifests are included in this component.

Deployment manifests need one optional environment variable when exposing the
new setting: `VERIFICATION_INTERVAL_SECONDS` (default `900`, minimum `60`). A
structural status schema must permit `status.lastVerifiedAt` as a date-time
string. Controller RBAC must grant `get`, `list`, `create`, and `delete` on
`batch/jobs`; `get`, `list`, `create`, and `delete` on core `configmaps`; `get`
on core `secrets`; and `list` on core `pods`. Pod deletion is not required
because Job owner garbage
collection removes historical Pods.
