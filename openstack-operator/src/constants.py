"""Constants used across the operator."""

# Tag/label used to identify operator-managed resources
MANAGED_BY_TAG = "managed-by-openstack-operator"

# Keystone project-tag namespace used for the single Git-authoritative contract.
CONTRACT_TAG_PREFIX = "contract:"

# Metadata-only GitOps trigger for repairing contract-tag drift.
CONTRACT_TAG_RECONCILE_ANNOTATION = "sunet.se/reconcile-contract-tag"

# Description prefix for resources that don't support tags
MANAGED_BY_DESCRIPTION_PREFIX = "[managed-by-openstack-operator] "
