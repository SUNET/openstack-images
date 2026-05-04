# Pin to specific version for reproducible builds.
# Update periodically for security patches.
FROM python:3.12.12-slim-trixie

LABEL org.opencontainers.image.source="https://github.com/SUNET/openstack-images"
LABEL org.opencontainers.image.description="SUNET customer portal (FastAPI)"

RUN apt-get update && apt-get install -y --no-install-recommends git && \
    rm -rf /var/lib/apt/lists/*

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

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
