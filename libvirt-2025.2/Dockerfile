# Libvirt 2025.2 with QEMU SPICE support
# Adds qemu-system-modules-spice for SPICE console graphics.
# Separate context from libvirt/ (2025.1) so both release lines get
# daily rebuilds while test and prod run different OpenStack releases.
FROM quay.io/airshipit/libvirt:2025.2-ubuntu_noble

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends qemu-system-modules-spice && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

USER libvirt
