# Pin to specific version for reproducible builds
# Update periodically for security patches
FROM python:3.12.12-slim-trixie

LABEL org.opencontainers.image.source="https://github.com/SUNET/openstack-images"
LABEL org.opencontainers.image.description="Kopf-based Kubernetes operator for managing OpenStack projects"

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user (use existing 'operator' group from base image)
RUN useradd -m -u 1000 -g operator operator

WORKDIR /app

# Copy all application files
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install Python package and dependencies
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check .

# Switch to non-root user
USER operator

# Run the operator
ENTRYPOINT ["kopf", "run", "--standalone", "--liveness=http://0.0.0.0:8080/healthz", "/app/src/handlers.py"]
