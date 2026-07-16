# Libvirt with QEMU SPICE support
# Adds qemu-system-modules-spice for SPICE console graphics
FROM quay.io/airshipit/libvirt:2025.1-ubuntu_noble

USER root

RUN apt-get update && \
    apt-get install -y --no-install-recommends qemu-system-modules-spice && \
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*

USER libvirt
