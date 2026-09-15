"""Upgrade real populated PostgreSQL 014 databases without rewriting tenant history.

The private server fixture is also imported by test_gitops_lifecycle. It uses a
unique Unix socket directory, never the developer's database or shared fixtures.
PostgreSQL is required: inability to start it is a failure, not a skipped test.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import URL, MetaData, create_engine, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.models import ClusterGitOps, CustomerClusterRepository, GitOpsOperation, TenantCluster

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LEGACY_TIME = datetime(2025, 4, 7, 12, 34, 56)


@dataclass(frozen=True)
class GitOpsDatabase:
    sync_url: URL

    @property
    def async_url(self) -> URL:
        return self.sync_url.set(drivername="postgresql+asyncpg")

    def migrate(self, revision: str) -> None:
        config = Config(str(PROJECT_ROOT / "alembic.ini"))
        config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
        escaped_url = self.sync_url.render_as_string(hide_password=False).replace("%", "%%")
        config.set_main_option("sqlalchemy.url", escaped_url)
        command.upgrade(config, revision)


@pytest.fixture(scope="session")
def gitops_postgres() -> Iterator[URL]:
    pg_config = shutil.which("pg_config")
    assert pg_config is not None, "Install PostgreSQL server tools in the test container"
    bindir = Path(subprocess.run(
        [pg_config, "--bindir"], check=True, capture_output=True, text=True,
    ).stdout.strip())
    for name in ("initdb", "pg_ctl"):
        assert (bindir / name).is_file(), f"Required PostgreSQL binary missing: {bindir / name}"
    with tempfile.TemporaryDirectory(prefix="gitops-pg-") as temporary:
        root = Path(temporary)
        data = root / "data"
        subprocess.run([
            str(bindir / "initdb"), "-D", str(data), "--no-locale", "--encoding=UTF8",
            "--auth=trust", "--username=portal",
        ], check=True, capture_output=True, text=True)
        ctl = [str(bindir / "pg_ctl"), "-D", str(data), "-w", "-t", "30"]
        try:
            subprocess.run([
                *ctl, "-l", str(root / "server.log"), "-o",
                f"-F -p 55433 -k {root} -h ''", "start",
            ], check=True, capture_output=True, text=True)
            yield URL.create(
                "postgresql+psycopg2", username="portal", database="postgres", port=55433,
                query={"host": str(root)},
            )
        finally:
            subprocess.run([*ctl, "-m", "immediate", "stop"], check=True, capture_output=True)


@pytest.fixture
def gitops_database(gitops_postgres: URL) -> Iterator[GitOpsDatabase]:
    name = f"gitops_{uuid4().hex}"
    admin = create_engine(gitops_postgres, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        yield GitOpsDatabase(gitops_postgres.set(database=name))
    finally:
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def seed_014(engine: Engine) -> MetaData:
    """Populate using reflected 014 tables, so newer ORM defaults cannot hide regressions."""
    metadata = MetaData()
    metadata.reflect(engine)
    with engine.begin() as connection:
        def insert(table: str, **values: Any) -> None:
            connection.execute(metadata.tables[table].insert().values(**values))

        for ident, name in ((10, "EOSC"), (20, "Other customer")):
            insert("customer", id=ident, name=name, domain=f"customer-{ident}.test",
                   description="Preserve customer metadata", created_at=LEGACY_TIME)
            insert("contract", id=ident, customer_id=ident, contract_number=f"CONTRACT-{ident}",
                   description="Preserve contract metadata", created_at=LEGACY_TIME)
        insert("contract_access", id=10, contract_id=10, user_sub="tenant@test",
               created_at=LEGACY_TIME)
        for ident, environment in ((10, "test"), (20, "prod")):
            insert("customer_cluster_repository", id=ident, customer_id=10,
                   environment=environment, repo_url=f"https://forgejo.test/eosc/{environment}.git",
                   writer_username=f"writer-{environment}",
                   reader_username=f"reader-{environment}",
                   created_at=LEGACY_TIME, updated_at=datetime(2026, 8, 1))
        insert("tenant_cluster", id=10, contract_id=10, name="Existing EOSC", slug="eosc-one",
               api_url="https://api.eosc-one.test:6443", ca_bundle="existing-ca",
               openbao_mount="kubernetes/eosc-one", argocd_alias="eosc.example.test",
               worker_groups=3, initial_worker_groups=2, provisioned_at=LEGACY_TIME,
               management_project_resource_name="eosc-management",
               backup_project_resource_name="eosc-backup", created_by_sub="old-admin@test",
               created_at=LEGACY_TIME)
        insert("tenant_cluster", id=20, contract_id=20, name="Planned", slug="planned",
               api_url=None, ca_bundle=None, openbao_mount="kubernetes/planned",
               created_by_sub="old-admin@test", created_at=LEGACY_TIME)
        insert("cluster_access", id=10, cluster_id=10, user_sub="tenant@test",
               role="customer_admin", granted_by_sub="old-admin@test", created_at=LEGACY_TIME)
        insert("kubeconfig_issuance", id=10, cluster_id=10, user_sub="tenant@test", label="Laptop",
               cert_serial="existing-serial", rolebinding_name="existing-binding",
               cert_group="eosc-operators", expires_at=datetime(2027, 1, 1),
               last_seen_at=datetime(2026, 9, 1), created_at=LEGACY_TIME)
        insert("cluster_addon", id=10, cluster_id=10, addon_type="jupyterhub",
               enabled_by_sub="old-admin@test", enabled_at=LEGACY_TIME)
        insert("cluster_request", id=10, cluster_id=10, request_type="resize",
               payload='{"worker_groups": 3, "previous_worker_groups": 2}', status="applied",
               requested_by_sub="tenant@test", applied_by_sub="old-admin@test",
               applied_at=datetime(2026, 8, 1), requested_at=LEGACY_TIME, note="Already billed")
        insert("resource_price", resource_type="fixture_metadata_price",
               unit_price=Decimal("42.17"), unit="month",
               metadata_field="tier", metadata_value="negotiated")
        insert("contract_price_override", id=10, contract_id=10,
               resource_type="cluster_setup_fee", unit_price=Decimal("1700.33"))
        insert("contract_rebate", id=10, contract_id=10, rebate_percent=Decimal("12.50"))
        insert("billing_job", id=10, name="Existing export", owner_sub="billing@test",
               all_contracts=False, schedule="0 6 1 * *", delivery_method="email",
               delivery_config='{"recipients":["billing@example.test"]}',
               filename_template="eosc-{year}-{month}.csv", per_contract=True, enabled=True,
               created_at=LEGACY_TIME)
        insert("billing_job_contract", id=10, billing_job_id=10, contract_id=10)
        insert("billing_job_run", id=10, billing_job_id=10, started_at=LEGACY_TIME,
               completed_at=LEGACY_TIME, billing_period_start=datetime(2025, 3, 1),
               billing_period_end=datetime(2025, 4, 1), status="success", files_delivered=1)
    return metadata


def legacy_rows(engine: Engine, metadata: MetaData) -> dict[str, list[dict[str, Any]]]:
    with engine.connect() as connection:
        return {
            table.name: [dict(row) for row in connection.execute(
                select(table).order_by(*table.primary_key.columns)
            ).mappings()]
            for table in metadata.sorted_tables if table.name != "alembic_version"
        }


@pytest.fixture
def populated_014(gitops_database: GitOpsDatabase) -> Iterator[tuple[Engine, MetaData]]:
    gitops_database.migrate("014")
    engine = create_engine(gitops_database.sync_url)
    try:
        yield engine, seed_014(engine)
    finally:
        engine.dispose()


def test_upgrade_014_to_015_preserves_all_existing_rows_and_metadata(
    gitops_database: GitOpsDatabase, populated_014: tuple[Engine, MetaData],
) -> None:
    engine, metadata = populated_014
    before = legacy_rows(engine, metadata)
    assert all(before.values()), "Each legacy table should contain preservation evidence"
    assert "config_version" not in metadata.tables["tenant_cluster"].columns
    gitops_database.migrate("015")
    assert legacy_rows(engine, metadata) == before
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT version_num FROM alembic_version")) == "015"
        assert connection.execute(text(
            "SELECT id, config_version FROM tenant_cluster ORDER BY id"
        )).all() == [(10, 1), (20, 1)]
        repositories = connection.execute(text(
            "SELECT version, writer_secret_version, reader_secret_version, writer_updated_at, "
            "reader_updated_at, validated_at, validation_status, validation_message "
            "FROM customer_cluster_repository ORDER BY id"
        )).all()
        assert repositories == [(1, None, None, None, None, None, "unvalidated", None)] * 2
        assert connection.scalar(text("SELECT count(*) FROM cluster_gitops")) == 0
        assert connection.scalar(text("SELECT count(*) FROM gitops_operation")) == 0
    gitops_database.migrate("015")
    assert legacy_rows(engine, metadata) == before


def test_migrated_models_round_trip_baseline_credentials_and_durable_operation(
    gitops_database: GitOpsDatabase, populated_014: tuple[Engine, MetaData],
) -> None:
    engine, _ = populated_014
    gitops_database.migrate("015")
    operation_id = str(uuid4())
    with Session(engine) as session:
        cluster = session.get(TenantCluster, 10)
        planned = session.get(TenantCluster, 20)
        repository = session.get(CustomerClusterRepository, 10)
        assert cluster.config_version == 1 and cluster.provisioned_at == LEGACY_TIME
        assert planned.api_url is planned.ca_bundle is planned.provisioned_at is None
        assert repository.writer_username == "writer-test" and repository.version == 1
        repository.writer_secret_version = 4
        repository.reader_secret_version = 7
        repository.version = 3
        repository.validation_status = "valid"
        repository.validation_message = "Verified API permission evidence"
        repository.writer_updated_at = repository.reader_updated_at = LEGACY_TIME
        repository.validated_at = LEGACY_TIME
        state = ClusterGitOps(cluster_id=10, repository_id=10, environment="test")
        operation = GitOpsOperation(
            id=operation_id, cluster_id=10, repository_id=10, kind="preview", status="queued",
            requested_by_sub="admin@test",
        )
        session.add_all([state, operation])
        session.commit()
        session.refresh(state)
        session.refresh(operation)
        assert state.version == 1 and state.baseline == "{}"
        assert operation.payload == "{}" and operation.created_at is not None
        state.baseline = '{"clusters/eosc-one/manifest.yaml":"preserved content\\n"}'
        state.last_commit = "a" * 64
        state.published_at = LEGACY_TIME
        state.reader_installed_version = 7
        operation.kind = "publish"
        operation.status = "succeeded"
        operation.payload = '{"settings_version":1,"repository_version":3}'
        operation.result_commit = state.last_commit
        operation.started_at = operation.finished_at = LEGACY_TIME
        session.commit()
    with Session(engine) as session:
        saved = session.get(ClusterGitOps, 10)
        operation = session.get(GitOpsOperation, operation_id)
        repository = session.get(CustomerClusterRepository, 10)
        assert "preserved content" in saved.baseline
        assert saved.last_commit == operation.result_commit == "a" * 64
        assert saved.reader_installed_version == repository.reader_secret_version == 7
        assert repository.writer_secret_version == 4 and repository.version == 3
        assert operation.status == "succeeded" and operation.finished_at == LEGACY_TIME
        assert session.get(TenantCluster, 10).provisioned_at == LEGACY_TIME


@pytest.mark.parametrize("child", ["cluster_gitops", "gitops_operation"])
@pytest.mark.parametrize("parent", ["tenant_cluster", "customer_cluster_repository"])
def test_history_foreign_keys_prevent_parent_deletion_without_cascading(
    gitops_database: GitOpsDatabase, populated_014: tuple[Engine, MetaData],
    child: str, parent: str,
) -> None:
    engine, _ = populated_014
    gitops_database.migrate("015")
    with engine.begin() as connection:
        if child == "cluster_gitops":
            connection.execute(text(
                "INSERT INTO cluster_gitops(cluster_id, repository_id, environment) "
                "VALUES (10, 10, 'test')"
            ))
        else:
            connection.execute(text(
                "INSERT INTO gitops_operation(id, cluster_id, repository_id, kind, status, "
                "requested_by_sub, result_commit) VALUES (:id, 10, 10, 'publish', "
                "'succeeded', 'admin@test', :commit)"
            ), {"id": str(uuid4()), "commit": "a" * 40})
    with pytest.raises(IntegrityError) as caught, engine.begin() as connection:
        connection.execute(text(f"DELETE FROM {parent} WHERE id = 10"))
    assert caught.value.orig.pgcode == "23503"
    assert caught.value.orig.diag.table_name == child
    with engine.connect() as connection:
        assert connection.scalar(text(f"SELECT count(*) FROM {child}")) == 1
        assert connection.scalar(text(f"SELECT count(*) FROM {parent} WHERE id = 10")) == 1
        assert connection.scalar(text("SELECT count(*) FROM cluster_access")) == 1
        assert connection.scalar(text("SELECT count(*) FROM cluster_request")) == 1


def test_new_tables_have_model_compatible_columns_defaults_keys_and_queue_index(
    gitops_database: GitOpsDatabase,
) -> None:
    gitops_database.migrate("015")
    engine = create_engine(gitops_database.sync_url)
    try:
        inspector = inspect(engine)
        for model in (ClusterGitOps, GitOpsOperation):
            actual = {
                column["name"]: column for column in inspector.get_columns(model.__tablename__)
            }
            assert set(actual) == set(model.__table__.columns.keys())
            for column in model.__table__.columns:
                assert actual[column.name]["type"]._type_affinity == column.type._type_affinity
                assert actual[column.name]["nullable"] == column.nullable, (
                    f"{model.__tablename__}.{column.name} differs from ORM nullability"
                )
            assert inspector.get_pk_constraint(model.__tablename__)["constrained_columns"] == [
                column.name for column in model.__table__.primary_key.columns
            ]
            assert {tuple(fk["constrained_columns"]) for fk in inspector.get_foreign_keys(
                model.__tablename__
            )} == {("cluster_id",), ("repository_id",)}
        assert any(index["column_names"] == ["status"] for index in inspector.get_indexes(
            "gitops_operation"
        ))
        assert next(column for column in inspector.get_columns("cluster_gitops")
                    if column["name"] == "baseline")["default"] == "'{}'::text"
    finally:
        engine.dispose()
