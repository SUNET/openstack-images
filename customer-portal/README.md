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
role binding. Customer admins are maintained separately as `reader` users in
the portal's default user domain. Self-service projects retain their existing
single customer `member` binding.
