FROM python:3.12.11-slim-trixie

LABEL org.opencontainers.image.source="https://github.com/SUNET/openstack-images"
LABEL org.opencontainers.image.description="ManagedCluster infrastructure operator and worker"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --gid operator --create-home operator

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src/ ./src/
RUN pip install --no-cache-dir --disable-pip-version-check .

USER operator
ENTRYPOINT ["customer-cluster-operator"]
