# Runner for the platform monitoring end-to-end tests (canary + hourly churn).
# Carries the OpenStack client (create/delete VMs, networks, routers, FIPs) and
# kubectl (publish results into a ConfigMap the naemon pod reads). The test
# scripts themselves live in a ConfigMap in platform-manifests, not here.
#
# Pin to a specific base for reproducible builds; update for security patches.
FROM python:3.12.12-slim-trixie

LABEL org.opencontainers.image.source="https://github.com/SUNET/openstack-images"
LABEL org.opencontainers.image.description="Runner for platform monitoring end-to-end tests (OpenStack client + kubectl)"

ARG KUBECTL_VERSION=v1.30.5

RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates bash \
    && curl -fsSL -o /usr/local/bin/kubectl \
        "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl" \
    && chmod +x /usr/local/bin/kubectl \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --root-user-action=ignore \
        --disable-pip-version-check python-openstackclient

# Runs unprivileged; the CronJob pins runAsUser and the scripts use HOME=/tmp.
RUN useradd -m -u 1000 runner
USER runner
ENTRYPOINT ["/bin/bash"]
