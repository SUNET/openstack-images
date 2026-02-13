# OpenStack Operator

A Kopf-based Kubernetes operator for declaratively managing OpenStack resources, including:

**Cluster-scoped resources:**
- Domains (Keystone)
- Flavors (Nova)
- Images (Glance) with web-download support
- Provider networks (Neutron)

**Namespace-scoped resources:**
- Projects and user groups
- Quotas (compute, storage, network)
- Networks, subnets, and routers
- Security groups and rules
- Role bindings
- Federation mappings for SSO access

## CRDs

### OpenstackDomain (cluster-scoped)

Manages Keystone domains for organizing users and projects.

```yaml
apiVersion: sunet.se/v1alpha1
kind: OpenstackDomain
metadata:
  name: sso-users
spec:
  name: sso-users
  description: "Domain for SSO-authenticated users"
  enabled: true
```

**Status:**
```yaml
status:
  phase: Ready
  domainId: "abc123..."
  conditions:
    - type: Ready
      status: "True"
```

### OpenstackFlavor (cluster-scoped)

Manages Nova flavors for VM sizing.

```yaml
apiVersion: sunet.se/v1alpha1
kind: OpenstackFlavor
metadata:
  name: b2.c2r4
spec:
  name: b2.c2r4
  vcpus: 2
  ram: 4096      # MB
  disk: 0        # GB (0 = boot from volume)
  ephemeral: 0   # GB
  swap: 0        # MB
  isPublic: true
  extraSpecs:
    "hw:cpu_policy": "shared"
```

**Status:**
```yaml
status:
  phase: Ready
  flavorId: "def456..."
  conditions:
    - type: Ready
      status: "True"
```

### OpenstackImage (cluster-scoped)

Manages Glance images with support for web-download import.

```yaml
apiVersion: sunet.se/v1alpha1
kind: OpenstackImage
metadata:
  name: debian-13
spec:
  name: "Debian 13 (Trixie)"
  visibility: public
  protected: false
  tags:
    - debian
    - trixie
  properties:
    os_distro: debian
    os_version: "13"
  content:
    diskFormat: qcow2
    containerFormat: bare
    source:
      url: https://cloud.debian.org/images/cloud/trixie/daily/latest/debian-13-generic-amd64-daily.qcow2
```

**Status:**
```yaml
status:
  phase: Ready
  imageId: "ghi789..."
  uploadStatus: active
  checksum: "abc123..."
  sizeBytes: 1234567890
  conditions:
    - type: Ready
      status: "True"
```

### OpenstackNetwork (cluster-scoped)

Manages provider networks (external/flat networks).

```yaml
apiVersion: sunet.se/v1alpha1
kind: OpenstackNetwork
metadata:
  name: external
spec:
  name: external
  external: true
  shared: false
  providerNetworkType: flat
  providerPhysicalNetwork: external
  subnets:
    - name: external-subnet
      cidr: 192.168.1.0/24
      gateway: 192.168.1.1
      enableDhcp: false
      allocationPools:
        - start: 192.168.1.100
          end: 192.168.1.200
```

**Status:**
```yaml
status:
  phase: Ready
  networkId: "jkl012..."
  subnets:
    - name: external-subnet
      subnetId: "mno345..."
  conditions:
    - type: Ready
      status: "True"
```

### OpenstackProject (namespace-scoped)

Manages projects with full resource provisioning.

```yaml
apiVersion: sunet.se/v1alpha1
kind: OpenstackProject
metadata:
  name: my-project
spec:
  name: "my-project.example.com"
  description: "My OpenStack project"
  domain: "sso-users"
  enabled: true

  quotas:
    compute:
      instances: 20
      cores: 40
      ramMB: 81920
    storage:
      volumes: 20
      volumesGB: 1000
    network:
      floatingIps: 10

  networks:
    - name: internal
      cidr: 192.168.100.0/24
      enableDhcp: true
      router:
        externalNetwork: external
        enableSnat: true

  securityGroups:
    - name: allow-ssh
      rules:
        - direction: ingress
          protocol: tcp
          portRangeMin: 22
          portRangeMax: 22
          remoteIpPrefix: "0.0.0.0/0"

  roleBindings:
    - role: member
      users:
        - user1@example.com
        - user2@example.com
      userDomain: sso-users

  federationRef:
    configMapName: federation-config
```

**Status:**
```yaml
status:
  phase: Ready
  projectId: "abc123..."
  groupId: "def456..."
  networks:
    - name: internal
      networkId: "..."
      subnetId: "..."
      routerId: "..."
  securityGroups:
    - name: allow-ssh
      id: "..."
  conditions:
    - type: ProjectReady
      status: "True"
    - type: NetworksReady
      status: "True"
```

## Building

```bash
# Build Docker image
docker build -t docker.sunet.se/platform/openstack-operator:latest .

# Or use Jenkins (see .jenkins.yaml)
```

## Deployment

The operator requires:

1. **CRDs installed** - All CRDs from `crds/`
2. **RBAC configured** - ServiceAccount with cluster-wide access to the CRDs
3. **Secrets** - See below

Example deployment using kustomize:

```bash
# From a deployment overlay
kubectl apply -k overlays/test/
```

### Secrets

| Secret | Key | Description | Required |
|--------|-----|-------------|----------|
| `openstack-operator-credentials` | `clouds.yaml` | OpenStack admin credentials (`clouds.yaml` format). Mounted at `/etc/openstack/clouds.yaml`. | Yes |
| `notification-transport-url` | `url` | AMQP URL for Keystone notification listener (see below). Format: `amqp://keystone:<password>@<rabbitmq-host>:5672/keystone` | No |

Both secrets should be sealed with kubeseal before committing:

```bash
kubeseal --format yaml < secret.yaml > sealed-secret.yaml
```

## Development

```bash
# Install dependencies
pip install -e ".[dev]"

# Run locally (requires kubeconfig and OpenStack credentials)
export OS_CLOUD=openstack
export OS_CLIENT_CONFIG_FILE=/path/to/clouds.yaml
kopf run src/handlers.py --standalone

# Run tests
pytest
```

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `OS_CLOUD` | Cloud name in clouds.yaml | `openstack` |
| `OS_CLIENT_CONFIG_FILE` | Path to clouds.yaml | Standard locations |
| `WATCH_NAMESPACE` | Namespace to watch (empty = all) | `""` |
| `NOTIFICATION_TRANSPORT_URL` | AMQP URL for Keystone notifications (optional) | Disabled |
| `METRICS_PORT` | Port for Prometheus metrics server | `9090` |
| `GC_INTERVAL_SECONDS` | Garbage collection interval | `600` |
| `MANAGED_DOMAIN` | Domain for GC scope | `sso-users` |

### Federation ConfigMap

For SSO integration, create a ConfigMap with IdP settings:

```yaml
apiVersion: v1
kind: ConfigMap
metadata:
  name: federation-config
data:
  idp-name: satosa
  idp-remote-id: https://idp.example.com
  sso-domain: sso-users
```

Reference it in OpenstackProject via `federationRef`.

### Keystone Notification Listener

When `NOTIFICATION_TRANSPORT_URL` is set, the operator subscribes to Keystone's
oslo.messaging notifications on RabbitMQ. When a new federated user is created
(shadow user on first SSO login), the operator immediately syncs them into the
relevant project groups instead of waiting for the next 5-minute reconciliation.

The URL should use the `keystone` RabbitMQ user (same credentials as in
Keystone's `transport_url`), with the `amqp://` scheme:

```
amqp://keystone:<password>@rabbitmq-rabbitmq-0.rabbitmq.openstack.svc.cluster.local:5672/keystone
```

If the secret is missing or the connection fails, the operator falls back to
the periodic reconciliation timer (every 300s).

## Resource Lifecycle

### Cluster-scoped Resources

- **Domains**: Created/updated on spec changes. Disabled before deletion.
- **Flavors**: Immutable after creation. Spec changes require delete+recreate.
- **Images**: Created with metadata, then web-download import triggered. Status polled until active.
- **Networks**: Provider networks with subnets. Subnets deleted before network on removal.

### Project Resources

- **Projects**: Created with associated user group. Quotas applied after creation.
- **Networks**: Project networks with optional router to external network.
- **Security Groups**: Created with rules. Default egress rules added automatically.
- **Federation**: Mapping created for SSO group-to-project binding.

## Garbage Collection

The operator tracks all managed resources in a ConfigMap (`openstack-operator-managed-resources`).
Orphaned resources (those without corresponding CRs) are automatically cleaned up.

## License

MIT
