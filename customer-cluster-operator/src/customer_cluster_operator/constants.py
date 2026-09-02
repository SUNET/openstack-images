"""Shared API and resource constants."""

API_GROUP = "customer-clusters.sunet.se"
API_VERSION = "v1alpha1"
PROFILE_PLURAL = "clusterprofiles"
CLUSTER_PLURAL = "managedclusters"
PROJECT_GROUP = "sunet.se"
PROJECT_VERSION = "v1alpha1"
PROJECT_PLURAL = "openstackprojects"
DEFAULT_PROFILE = "standard-v1"
MANAGED_BY = "customer-cluster-operator"
INPUT_MOUNT = "/var/run/customer-cluster/input"
SSH_MOUNT = "/var/run/customer-cluster/ssh"
CLOUDS_MOUNT = "/etc/openstack/clouds.yaml"
MAX_MESSAGE = 512
JOB_HISTORY_LIMIT = 2
