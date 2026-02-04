# OpenStack Placement with oslo-secrets-wrapper
# Allows loading sensitive config from Kubernetes Secrets
FROM quay.io/airshipit/placement:2025.1-ubuntu_noble

USER root

COPY oslo-secrets-wrapper.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/oslo-secrets-wrapper.sh

USER placement

ENTRYPOINT ["/usr/local/bin/oslo-secrets-wrapper.sh"]
