# Pin the multi-architecture index; both stages use Docker's target platform.
FROM golang:1.26.8-trixie@sha256:771f3162219f7a66bb6be3fea1ba31f418932946a99ad9eac26ffd555dfcf393 AS kustomize-builder

ENV CGO_ENABLED=0 \
    GOTOOLCHAIN=local \
    GOPROXY=https://proxy.golang.org \
    GOSUMDB=sum.golang.org

# The fixed module and its dependencies are verified through Go's checksum database.
RUN go install -trimpath -ldflags="-s -w" sigs.k8s.io/kustomize/kustomize/v5@v5.8.0

# Pin to specific version for reproducible builds.
# Update periodically for security patches.
FROM python:3.12.12-slim-trixie

LABEL org.opencontainers.image.source="https://github.com/SUNET/openstack-images"
LABEL org.opencontainers.image.description="SUNET customer portal (FastAPI)"

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

# Jenkins uses the legacy builder; COPY --chmod requires BuildKit.
COPY --from=kustomize-builder /go/bin/kustomize /usr/local/bin/kustomize
RUN chmod 0555 /usr/local/bin/kustomize

# Create non-root user.
RUN groupadd -r portal && useradd -r -m -u 1000 -g portal portal

WORKDIR /app

COPY pyproject.toml .
RUN pip install --no-cache-dir --root-user-action=ignore --disable-pip-version-check .

COPY app/ app/
COPY static/ static/
COPY alembic/ alembic/
COPY alembic.ini .

# Pre-create the git workdir under /app (so it's writable when we mount an
# emptyDir there) and chown everything to the portal user.
RUN mkdir -p /app/git-work && chown -R portal:portal /app

USER portal

# Fail the image build if the preview prerequisite is missing or unusable as portal.
RUN test "$(command -v kustomize)" = "/usr/local/bin/kustomize" && \
    test "$(kustomize version)" = "v5.8.0"

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
