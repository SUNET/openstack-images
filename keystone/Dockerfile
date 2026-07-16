# OpenStack Keystone with OIDC support
# Based on airshipit keystone image with libapache2-mod-auth-openidc added
FROM quay.io/airshipit/keystone:2025.1-ubuntu_noble

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends libapache2-mod-auth-openidc && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

# Enable the module
RUN a2enmod auth_openidc

# Add wrapper script that generates OIDC secrets config before starting Apache
COPY apache2-oidc-wrapper.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/apache2-oidc-wrapper.sh

# Add WSGI script with friendly error pages for browser 401 responses
COPY wsgi-friendly.py /usr/local/share/keystone/

USER keystone
