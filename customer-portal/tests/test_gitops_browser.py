"""Exercise the real SPA against a local static server and an in-browser mock API.

No portal server, database, Git remote or cluster is used. Install Python Playwright
and its Chromium browser to run; PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH can select an
existing Chromium installation. Missing browser tooling produces an explicit skip.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass, field
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from typing import Any
from urllib.parse import urlsplit

import pytest

pytest.importorskip("playwright.sync_api", reason="GitOps browser tests require Python Playwright")
from playwright.sync_api import Browser, Error, Page, Route, expect, sync_playwright  # noqa: E402

STATIC = Path(__file__).parents[1] / "static"
REPOSITORY = "/api/admin/customers/7/cluster-repository"
CLUSTER = "/api/admin/clusters/eosc-one"
GITOPS = CLUSTER + "/gitops"
STAMP = "2026-09-15T12:00:00Z"
DIFF = "--- a/cluster.yaml\n+++ b/cluster.yaml\n+<img src=x onerror=alert('unsafe')>\n"
DEFAULT_BASES = "f" * 40
PREVIEW_BASES = "a" * 40
EXPECTED_HEAD = "b" * 40
SOURCE_INVENTORY_COMMIT = "c" * 40
JsonObject = dict[str, Any]


def prepared_metadata(slug: str) -> JsonObject:
    return {
        "bases_revision": PREVIEW_BASES,
        "expected_head": EXPECTED_HEAD,
        "source": {
            "namespace": "clusters",
            "slug": slug,
            "uid": "f5f0636d-43be-472e-82d8-828310661062",
            "generation": 4,
            "inventory_commit": SOURCE_INVENTORY_COMMIT,
            "inventory_path": f"clusters/{slug}/generated/ansible/hosts.yml",
            "hostname": f"{slug}.k8s-test.sunetvdc.se",
            "ingress_vip": "192.0.2.10",
            "interface": "ens3",
        },
        "validation": {
            "kustomize_version": "v5.6.0",
            "kustomizations": [
                f"clusters/{slug}/addons/argocd-ingress",
                f"clusters/{slug}/argocd-apps",
                "k8s-manifests/envoy-gateway",
            ],
            "envoy_protections": True,
        },
    }


def cluster_record(slug: str = "eosc-one", *, live: bool = False) -> JsonObject:
    return {
        "id": 1 if slug == "eosc-one" else 2,
        "customer_id": 7,
        "environment": "test",
        "config_version": 7,
        "contract_number": "EOSC-1",
        "slug": slug,
        "name": "EOSC one" if slug == "eosc-one" else "EOSC two",
        "worker_groups": 1,
        "initial_worker_groups": 1,
        "total_servers": 6,
        "size_label": "Liten",
        "provisioned_at": STAMP if live else None,
        "api_url": f"https://{slug}.k8s-test.sunetvdc.se:6443" if live else None,
        "api_hostname": f"{slug}.k8s-test.sunetvdc.se",
        "argocd_hostname": f"argocd.{slug}.k8s-test.sunetvdc.se",
        "argocd_alias": "argocd.eosc.example.org",
        "argocd_namespace": "argocd",
        "openbao_secret_root": f"secret/data/customers/test/eosc/clusters/{slug}",
        "manifest_path": f"clusters/{slug}/cluster.yaml",
        "management_project_resource_name": f"{slug}.example.org",
        "backup_project_resource_name": None,
        "connection_configured": live,
        "active_addons": [],
        "created_at": STAMP,
        "caller_role": "sunet_admin",
    }


def missing_repository() -> JsonObject:
    return {
        "customer_id": 7,
        "environment": "test",
        "configured": False,
        "id": None,
        "version": 0,
        "repo_url": None,
        "writer_username": None,
        "reader_username": None,
        "writer_configured": False,
        "reader_configured": False,
        "writer_secret_version": None,
        "reader_secret_version": None,
        "writer_updated_at": None,
        "reader_updated_at": None,
        "validation_status": "unvalidated",
        "validation_message": None,
        "validated_at": None,
        "clusters": [],
        "bases_revision": DEFAULT_BASES,
    }


@dataclass(frozen=True)
class ApiCall:
    method: str
    path: str
    body: JsonObject


@dataclass
class MockApi:
    repository: JsonObject = field(default_factory=missing_repository)
    clusters: dict[str, JsonObject] = field(default_factory=lambda: {"eosc-one": cluster_record()})
    drafts: dict[str, JsonObject] = field(default_factory=dict)
    operations: dict[str, JsonObject] = field(default_factory=dict)
    calls: list[ApiCall] = field(default_factory=list)
    errors: dict[tuple[str, str], tuple[int, JsonObject]] = field(default_factory=dict)
    secret_versions: dict[str, int] = field(default_factory=dict)
    polls: dict[str, int] = field(default_factory=dict)
    infrastructure: JsonObject = field(default_factory=dict)
    hold_operations: bool = False
    preview_outcome: str = "preview_ready"
    preview_diff: str = DIFF
    preview_head: str | None = EXPECTED_HEAD
    lose_publish_response: bool = False
    publish_outcomes: list[str] = field(default_factory=lambda: ["succeeded"])
    unhandled: list[str] = field(default_factory=list)

    def ready(self) -> None:
        self.repository.update(
            configured=True,
            id=3,
            version=4,
            repo_url="https://git.example.org/eosc/clusters-test.git",
            writer_username="eosc-writer",
            reader_username="eosc-reader",
            writer_configured=True,
            reader_configured=True,
            writer_secret_version=2,
            reader_secret_version=3,
            writer_updated_at=STAMP,
            reader_updated_at=STAMP,
            validation_status="valid",
            validation_message="Repository and writer validated",
            validated_at=STAMP,
        )

    def matching(self, method: str, path: str) -> list[ApiCall]:
        return [call for call in self.calls if call.method == method and call.path == path]

    def draft(self, slug: str) -> JsonObject:
        return self.drafts.setdefault(
            slug,
            {
                "version": 1,
                "acme_contact": "ops@example.org",
                "reader_installed_version": None,
                "last_commit": None,
                "published_at": None,
            },
        )

    def repository_response(self) -> JsonObject:
        return {
            **self.repository,
            "clusters": [
                {
                    "slug": slug,
                    "name": cluster["name"],
                    "reader_installed_version": self.draft(slug)["reader_installed_version"],
                }
                for slug, cluster in self.clusters.items()
            ],
        }

    def gitops_response(self, slug: str) -> JsonObject:
        infrastructure = {
            "status": "ready",
            "namespace": "clusters",
            "name": slug,
            "phase": "VirtualMachinesReady",
            "reason": "InfrastructureReady",
            "message": "Infrastructure and inventory verified",
            "inventory_path": f"clusters/{slug}/generated/ansible/hosts.yml",
            "inventory_commit": SOURCE_INVENTORY_COMMIT,
            **self.infrastructure,
        }
        blockers = []
        if not self.repository["configured"]:
            blockers.append({"code": "repository_missing", "message": "Configure the repository"})
        elif not self.repository["writer_configured"]:
            blockers.append({"code": "writer_missing", "message": "Configure a writer credential"})
        elif self.repository["validation_status"] != "valid":
            blockers.append(
                {"code": "repository_unvalidated", "message": "Validate the repository"}
            )
        if infrastructure["status"] != "ready":
            blockers.append(
                {
                    "code": f"infrastructure_{infrastructure['status']}",
                    "message": infrastructure["message"],
                }
            )
        return {
            "customer_id": 7,
            "environment": "test",
            "repository_id": self.repository["id"],
            **self.draft(slug),
            "infrastructure": infrastructure,
            "can_preview": not blockers,
            "blockers": blockers,
            "operations": [
                op for op in reversed(list(self.operations.values())) if op["cluster_slug"] == slug
            ],
        }

    def poll(self, operation_id: str) -> JsonObject:
        operation = self.operations[operation_id]
        self.polls[operation_id] = self.polls.get(operation_id, 0) + 1
        if operation["status"] not in ("queued", "running"):
            return operation
        if self.hold_operations or self.polls[operation_id] == 1:
            operation["status"] = "running"
            return operation
        if operation["kind"] == "preview":
            operation["status"] = self.preview_outcome
            if self.preview_outcome == "preview_ready":
                operation.update(prepared_metadata(operation["cluster_slug"]))
                operation.update(
                    diff=self.preview_diff,
                    expected_head=self.preview_head,
                    action="noop"
                    if self.preview_diff == ""
                    else ("initialize" if self.preview_head is None else "add"),
                )
        else:
            operation["status"] = self.publish_outcomes.pop(0)
            if operation["status"] == "succeeded":
                operation["result_commit"] = "published-456"
                self.draft(operation["cluster_slug"]).update(
                    last_commit="published-456", published_at=STAMP
                )
        if operation["status"] in ("failed", "conflict"):
            operation.update(
                error_code="remote_unavailable"
                if operation["status"] == "failed"
                else "stale_head",
                error_message="Git operation could not complete",
                request_id="worker-request-17",
            )
        return operation

    def handle(self, route: Route) -> None:
        request = route.request
        path = urlsplit(request.url).path
        method = request.method
        body = (request.post_data_json or {}) if request.post_data else {}
        self.calls.append(
            ApiCall(method, path, {**body, **({"token": "[redacted]"} if "token" in body else {})})
        )
        if error := self.errors.pop((method, path), None):
            route.fulfill(status=error[0], json=error[1])
            return
        if path == "/api/me":
            route.fulfill(
                json={
                    "sub": "operator@test",
                    "name": "Test operator",
                    "is_admin": True,
                    "contracts": [],
                }
            )
        elif path == "/api/admin/contracts":
            route.fulfill(json=[{"id": 1, "customer_id": 7, "contract_number": "EOSC-1"}])
        elif path in ("/api/admin/customers", "/api/admin/customers/7"):
            customer = {
                "id": 7,
                "name": "EOSC",
                "domain": "example.org",
                "description": "",
                "created_at": STAMP,
                "contracts": [{"id": 1, "contract_number": "EOSC-1", "created_at": STAMP}],
            }
            route.fulfill(json=[customer] if path.endswith("customers") else customer)
        elif path.startswith(REPOSITORY):
            self.handle_repository(route, path, method, body)
        elif path.startswith("/api/admin/gitops-operations/"):
            route.fulfill(json=self.poll(path.rsplit("/", 1)[1]))
        elif path == "/api/admin/clusters":
            if method == "POST":
                assert self.repository["validation_status"] == "valid"
                assert set(body) <= {
                    "name",
                    "slug",
                    "contract_number",
                    "worker_groups",
                    "argocd_alias",
                }
                created = {**cluster_record(body["slug"]), **body}
                self.clusters[body["slug"]] = created
                route.fulfill(status=201, json=created)
            else:
                route.fulfill(json=list(self.clusters.values()))
        elif path.startswith("/api/admin/clusters/"):
            self.handle_cluster(route, path, method, body)
        else:
            self.unhandled.append(f"{method} {path}")
            route.fulfill(status=500, json={"detail": "Unexpected mock request"})

    def handle_repository(self, route: Route, path: str, method: str, body: JsonObject) -> None:
        if method == "GET":
            route.fulfill(json=self.repository_response())
            return
        assert body["expected_version"] == self.repository["version"]
        if method == "PUT":
            assert set(body) == {"repo_url", "expected_version"}
            self.repository.update(repo_url=body["repo_url"], id=3, configured=True)
        elif "/credentials/" in path:
            kind = path.rsplit("/", 1)[1]
            assert {"expected_version", "username", "token"} <= set(body)
            assert set(body) <= {
                "expected_version",
                "username",
                "token",
                "expected_secret_version",
            }
            assert body["username"].strip() and body["token"].strip()
            pinned = self.repository[f"{kind}_secret_version"]
            cas = body.get("expected_secret_version", pinned or 0)
            assert type(cas) is int and cas >= 0
            latest = self.secret_versions.get(kind, pinned or 0)
            if cas != latest:
                route.fulfill(
                    status=409,
                    json={
                        "detail": {
                            "code": "secret_version_conflict",
                            "kind": kind,
                            "repository_version": self.repository["version"],
                            "pinned_secret_version": pinned,
                            "expected_secret_version": cas,
                            "latest_secret_version": latest if kind == "writer" else None,
                        },
                        "request_id": "cas-request-2",
                    },
                )
                return
            self.secret_versions[kind] = cas + 1
            self.repository.update(
                {
                    f"{kind}_username": body["username"],
                    f"{kind}_configured": True,
                    f"{kind}_secret_version": cas + 1,
                    f"{kind}_updated_at": STAMP,
                }
            )
        elif path.endswith("/validate"):
            assert self.repository["writer_configured"]
            self.repository.update(validation_status="valid", validated_at=STAMP)
        else:
            raise AssertionError(f"Unexpected repository action: {method} {path}")
        if not path.endswith("/validate"):
            self.repository["validation_status"] = "unvalidated"
        self.repository["version"] += 1
        route.fulfill(json=self.repository_response())

    def handle_cluster(self, route: Route, path: str, method: str, body: JsonObject) -> None:
        parts = path.split("/")
        slug = parts[4]
        cluster = self.clusters[slug]
        if len(parts) == 5:
            if method == "PATCH":
                assert set(body) <= {
                    "name",
                    "argocd_alias",
                    "api_url",
                    "ca_bundle",
                    "config_version",
                }
                assert body["config_version"] == cluster["config_version"]
                cluster.update({key: value for key, value in body.items() if key != "ca_bundle"})
                cluster["config_version"] += 1
            route.fulfill(json=cluster)
            return
        assert parts[5] == "gitops"
        action = parts[6] if len(parts) > 6 else ""
        if method == "GET":
            route.fulfill(json=self.gitops_response(slug))
        elif method == "PUT":
            assert body["expected_version"] == self.draft(slug)["version"]
            self.draft(slug).update(
                acme_contact=body["acme_contact"], version=body["expected_version"] + 1
            )
            route.fulfill(json=self.gitops_response(slug))
        elif action == "reader-installed":
            assert set(body) == {"version"}
            self.draft(slug)["reader_installed_version"] = body["version"]
            route.fulfill(json=self.gitops_response(slug))
        elif action == "preview":
            assert set(body) == {"adopt"}
            operation_id = f"op-{len(self.operations) + 1}"
            operation = {
                "id": operation_id,
                "cluster_slug": slug,
                "kind": "preview",
                "status": "queued",
                "error_code": None,
                "error_message": None,
                "result_commit": None,
                "diff": None,
                "action": None,
                "bases_revision": None,
                "expected_head": None,
                "source": None,
                "validation": None,
                "created_at": STAMP,
            }
            self.operations[operation_id] = operation
            route.fulfill(status=202, json=operation)
        elif action == "publish":
            assert set(body) == {"operation_id"}
            operation = self.operations[body["operation_id"]]
            if operation["status"] == "succeeded":
                route.fulfill(json=operation)
                return
            assert operation["status"] == "preview_ready" or (
                operation["status"] == "failed" and operation["kind"] == "publish"
            )
            operation.update(kind="publish", status="queued", error_code=None, error_message=None)
            if self.lose_publish_response:
                self.lose_publish_response = False
                operation.update(status="succeeded", result_commit="published-456")
                self.draft(slug).update(last_commit="published-456", published_at=STAMP)
                route.abort("failed")
                return
            self.polls[operation["id"]] = 0
            route.fulfill(status=202, json=operation)
        else:
            raise AssertionError(f"Unexpected cluster action: {method} {path}")


class QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """Keep request logging out of browser test output."""


@pytest.fixture(scope="module")
def static_url() -> Iterator[str]:
    handler = partial(QuietStaticHandler, directory=str(STATIC))
    with ThreadingHTTPServer(("127.0.0.1", 0), handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture(scope="module")
def browser() -> Iterator[Browser]:
    with sync_playwright() as playwright:
        executable = os.environ.get("PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH")
        if not executable and not Path(playwright.chromium.executable_path).is_file():
            executable = shutil.which("chromium") or shutil.which("chromium-browser")
            if not executable:
                pytest.skip(
                    "Chromium unavailable: run playwright install chromium or set "
                    "PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH"
                )
        try:
            instance = playwright.chromium.launch(headless=True, executable_path=executable)
        except Error as error:
            message = str(error)
            missing_library = next(
                (line for line in message.splitlines() if "error while loading" in line),
                message.splitlines()[0],
            )
            pytest.skip(f"Chromium cannot launch: {missing_library}")
        try:
            yield instance
        finally:
            instance.close()


@dataclass
class Portal:
    page: Page
    api: MockApi
    base_url: str

    def open(self, route: str = "/admin/clusters/eosc-one") -> None:
        self.page.goto(f"{self.base_url}/#{route}")
        self.page.wait_for_load_state("networkidle")


@pytest.fixture
def portal(browser: Browser, static_url: str) -> Iterator[Portal]:
    api = MockApi()
    errors: list[str] = []
    with browser.new_context(viewport={"width": 1280, "height": 1000}) as context:
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.route("**/api/**", api.handle)
        yield Portal(page, api, static_url)
        assert not errors
        assert not api.unhandled


def test_existing_cluster_configures_shared_repository_on_customer_routes(portal: Portal) -> None:
    page, api = portal.page, portal.api
    portal.open()
    expect(page.get_by_role("button", name="Generate preview", exact=True)).to_be_disabled()
    page.get_by_role("button", name="Configure repository", exact=True).click()
    page.get_by_label("Shared repository URL", exact=True).fill(
        "https://git.example.org/eosc/clusters.git"
    )
    page.get_by_role("button", name="Save repository URL", exact=True).click()
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Shared repository URL saved"
    )
    assert api.matching("PUT", REPOSITORY)[0].body["expected_version"] == 0

    page.get_by_label("Writer username", exact=True).fill("eosc-writer")
    page.get_by_label("New writer token", exact=True).fill("synthetic-writer-token")
    page.get_by_role("button", name="Replace writer credential", exact=True).click()
    expect(page.get_by_label("New writer token", exact=True)).to_have_value("")
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Writer credential replaced"
    )
    page.get_by_role("button", name="Validate repository", exact=True).click()
    expect(page.get_by_role("button", name="Generate preview", exact=True)).to_be_enabled()

    page.get_by_role("link", name="Customer 7", exact=True).click()
    expect(page.get_by_role("heading", name="EOSC", exact=True)).to_be_visible()
    page.get_by_text("Edit shared repository", exact=True).click()
    expect(page.get_by_label("Shared repository URL", exact=True)).to_have_value(
        "https://git.example.org/eosc/clusters.git"
    )
    expect(page.locator(".repository-clusters")).to_contain_text("eosc-one")
    page.get_by_role("link", name="Edit", exact=True).click()
    page.get_by_text("Edit shared repository", exact=True).click()
    page.get_by_label("Shared repository URL", exact=True).fill(
        "https://git.example.org/eosc/clusters-updated.git"
    )
    page.get_by_role("button", name="Save repository URL", exact=True).click()
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Shared repository URL saved"
    )
    assert not api.matching("PATCH", "/api/admin/customers/7")


def test_second_cluster_reuses_writer_without_any_tokens(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    portal.open("/admin/clusters/new")
    create = page.get_by_role("button", name="Create and start provisioning", exact=True)
    expect(create).to_be_disabled()
    expect(page.locator("input[type=password]")).to_have_count(0)
    page.get_by_label("Contract", exact=True).select_option("EOSC-1")
    expect(create).to_be_enabled()
    expect(page.get_by_label("Selected customer repository")).to_contain_text(
        "No tokens are needed here"
    )
    page.get_by_label("Portal display name", exact=True).fill("EOSC two")
    page.get_by_label("Slug (used in OpenBao mount path & cert O)", exact=True).fill("eosc-two")
    create.evaluate("button => { button.click(); button.click(); }")
    expect(page.get_by_role("heading", name="EOSC two", exact=True)).to_be_visible()
    created = api.matching("POST", "/api/admin/clusters")
    assert len(created) == 1
    assert created[0].body == {
        "contract_number": "EOSC-1",
        "name": "EOSC two",
        "slug": "eosc-two",
        "worker_groups": 1,
        "argocd_alias": None,
    }
    assert not [call for call in api.calls if "/credentials/" in call.path]


def test_creation_blocks_missing_unvalidated_and_unavailable_repository(portal: Portal) -> None:
    page, api = portal.page, portal.api
    portal.open("/admin/clusters/new")
    page.get_by_label("Contract", exact=True).select_option("EOSC-1")
    create = page.get_by_role("button", name="Create and start provisioning", exact=True)
    expect(create).to_be_disabled()
    expect(
        page.get_by_role("link", name="Configure shared repository", exact=True)
    ).to_have_attribute("href", "#/admin/customers/7")
    api.ready()
    api.repository["validation_status"] = "unvalidated"
    page.get_by_role("button", name="Refresh repository status", exact=True).click()
    expect(create).to_be_disabled()
    api.errors[("GET", REPOSITORY)] = (
        503,
        {"detail": "Repository lookup unavailable", "request_id": "repo-request-1"},
    )
    page.get_by_role("button", name="Refresh repository status", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("repo-request-1")
    expect(create).to_be_disabled()
    assert not api.matching("POST", "/api/admin/clusters")


def test_blank_credentials_are_preserved_and_replacements_are_independent(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    portal.open("/admin/customers/7")
    page.get_by_text("Edit shared repository", exact=True).click()
    token = page.get_by_label("New writer token", exact=True)
    expect(token).to_have_value("")
    expect(token).to_have_attribute("autocomplete", "new-password")
    page.get_by_label("Writer username", exact=True).fill("different-writer")
    page.get_by_role("button", name="Replace writer credential", exact=True).click()
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Writer credential kept"
    )
    page.get_by_role("button", name="Replace reader credential", exact=True).click()
    page.get_by_label("Shared repository URL", exact=True).fill("")
    page.get_by_role("button", name="Save repository URL", exact=True).click()
    assert not [call for call in api.calls if call.method != "GET"]

    page.get_by_label("Writer username", exact=True).fill("")
    token.fill("synthetic-new-token")
    page.get_by_role("button", name="Replace writer credential", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("both a non-empty username and token")
    assert not api.matching("POST", REPOSITORY + "/credentials/writer")
    page.get_by_label("Writer username", exact=True).fill("new-writer")
    page.get_by_role("button", name="Replace writer credential", exact=True).click()
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Writer credential replaced"
    )
    assert api.matching("POST", REPOSITORY + "/credentials/writer")[0].body == {
        "expected_version": 4,
        "username": "new-writer",
        "token": "[redacted]",
    }
    assert not api.matching("POST", REPOSITORY + "/credentials/reader")
    expect(token).to_have_value("")
    assert (
        page.evaluate("Object.keys(localStorage).length + Object.keys(sessionStorage).length") == 0
    )


def test_live_connection_and_display_editor_omit_unchanged_and_blank_fields(
    portal: Portal,
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.clusters["eosc-one"] = cluster_record(live=True)
    portal.open()
    expect(page.get_by_label("API URL", exact=True)).to_be_visible()
    page.get_by_label("Portal display name", exact=True).fill("EOSC production")
    page.get_by_label("Requested Argo CD DNS alias", exact=True).fill("new-alias.example.org")
    page.get_by_role("button", name="Save cluster settings", exact=True).click()
    expect(page.get_by_role("heading", name="EOSC production", exact=True)).to_be_visible()
    assert api.matching("PATCH", CLUSTER)[0].body == {
        "name": "EOSC production",
        "argocd_alias": "new-alias.example.org",
        "config_version": 7,
    }
    page.get_by_role("button", name="Save connection details", exact=True).click()
    assert len(api.matching("PATCH", CLUSTER)) == 1
    page.get_by_label("CA bundle (PEM)", exact=True).fill(
        "-----BEGIN CERTIFICATE-----\nreplacement\n-----END CERTIFICATE-----"
    )
    page.get_by_role("button", name="Save connection details", exact=True).click()
    expect(page.get_by_label("CA bundle (PEM)", exact=True)).to_have_value("")
    assert set(api.matching("PATCH", CLUSTER)[1].body) == {"ca_bundle", "config_version"}
    assert api.matching("PATCH", CLUSTER)[1].body["config_version"] == 8
    page.get_by_label("API URL", exact=True).fill("https://replacement.example.org:6443")
    page.get_by_role("button", name="Save connection details", exact=True).click()
    settings_status = page.get_by_label("Cluster settings", exact=True).get_by_role("status")
    expect(settings_status).to_contain_text("Kubernetes connection saved")
    expect(page.get_by_label("Cluster overview", exact=True)).to_contain_text(
        "https://replacement.example.org:6443"
    )
    assert api.matching("PATCH", CLUSTER)[2].body == {
        "api_url": "https://replacement.example.org:6443",
        "config_version": 9,
    }
    page.get_by_label("Portal display name", exact=True).fill("")
    page.get_by_label("Requested Argo CD DNS alias", exact=True).fill("")
    page.get_by_role("button", name="Save cluster settings", exact=True).click()
    expect(settings_status).to_contain_text("No changes to save")
    assert len(api.matching("PATCH", CLUSTER)) == 3
    assert page.locator('input[name="slug"], input[name="openbao_role"]').count() == 0


def test_preview_requires_review_and_explicit_publish_then_survives_reload(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    portal.open()
    page.get_by_label("ACME contact email", exact=True).fill("new-ops@example.org")
    preview = page.get_by_role("button", name="Generate preview", exact=True)
    expect(preview).to_be_disabled()
    page.get_by_role("button", name="Save GitOps draft", exact=True).click()
    expect(preview).to_be_enabled()
    assert api.matching("PUT", GITOPS)[0].body == {
        "expected_version": 1,
        "acme_contact": "new-ops@example.org",
    }
    page.get_by_label("Adopt existing GitOps files in this preview", exact=True).check()
    preview.evaluate("button => { button.click(); button.click(); }")
    expect(preview).to_be_disabled()
    expect(page.get_by_label("GitOps diff", exact=True)).to_have_text(DIFF, timeout=10000)
    assert api.matching("POST", GITOPS + "/preview")[0].body == {"adopt": True}
    assert len(api.matching("POST", GITOPS + "/preview")) == 1
    assert not api.matching("POST", GITOPS + "/publish")
    expect(page.locator(".gitops-diff img")).to_have_count(0)
    page.once("dialog", lambda dialog: dialog.dismiss())
    page.get_by_role("button", name="Publish reviewed diff", exact=True).click()
    assert not api.matching("POST", GITOPS + "/publish")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Publish reviewed diff", exact=True).click()
    expect(page.get_by_label("Lifecycle status", exact=True)).to_contain_text(
        "published-456", timeout=10000
    )
    expect(page.get_by_label("Lifecycle status", exact=True)).to_contain_text("Not activated")
    expect(page.get_by_label("Lifecycle status", exact=True)).to_contain_text(
        "Infrastructure and inventory verified"
    )
    assert api.matching("POST", GITOPS + "/publish")[0].body == {"operation_id": "op-1"}
    page.reload()
    expect(page.get_by_label("GitOps operation history", exact=True)).to_contain_text("succeeded")
    expect(page.get_by_label("GitOps diff", exact=True)).to_have_text(DIFF)
    assert len(api.matching("POST", GITOPS + "/publish")) == 1


def test_pending_history_resumes_and_transient_publish_retries_same_operation(
    portal: Portal,
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.hold_operations = True
    api.publish_outcomes = ["failed", "succeeded"]
    portal.open()
    page.get_by_role("button", name="Generate preview", exact=True).click()
    expect(page.get_by_label("Operation review", exact=True)).to_contain_text(
        "running", timeout=10000
    )
    page.reload()
    expect(page.get_by_label("GitOps operation history", exact=True)).to_contain_text("running")
    api.hold_operations = False
    expect(page.get_by_label("GitOps diff", exact=True)).to_have_text(DIFF, timeout=10000)
    api.errors[("POST", GITOPS + "/publish")] = (
        503,
        {"detail": {"message": "Queue unavailable", "request_id": "publish-request-42"}},
    )
    page.on("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Publish reviewed diff", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("publish-request-42")
    page.get_by_role("button", name="Publish reviewed diff", exact=True).click()
    expect(page.get_by_label("Operation review", exact=True)).to_contain_text(
        "remote_unavailable", timeout=10000
    )
    expect(page.get_by_label("Operation review", exact=True)).to_contain_text("worker-request-17")
    page.get_by_role("button", name="Retry approved publish", exact=True).click()
    expect(page.get_by_label("Lifecycle status", exact=True)).to_contain_text(
        "published-456", timeout=10000
    )
    assert [call.body for call in api.matching("POST", GITOPS + "/publish")] == [
        {"operation_id": "op-1"}
    ] * 3
    assert len(api.matching("POST", GITOPS + "/preview")) == 1


@pytest.mark.parametrize("outcome", ["failed", "conflict"])
def test_failed_preview_and_conflict_are_inline_and_cannot_publish(
    portal: Portal, outcome: str
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.preview_outcome = outcome
    portal.open()
    page.get_by_role("button", name="Generate preview", exact=True).click()
    expect(page.get_by_label("Operation review", exact=True)).to_contain_text(
        "worker-request-17", timeout=10000
    )
    expect(page.get_by_role("button", name="Publish reviewed diff", exact=True)).to_have_count(0)
    assert not api.matching("POST", GITOPS + "/publish")


def test_navigation_cancels_poll_timer_and_does_not_publish(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.hold_operations = True
    page.add_init_script("""(() => {
        const timers = window.__pollTimers = new Set();
        const start = window.setTimeout.bind(window);
        const stop = window.clearTimeout.bind(window);
        window.setTimeout = (callback, delay, ...args) => {
            const id = start(() => { timers.delete(id); callback(...args); }, delay);
            if (delay === 1000) timers.add(id);
            return id;
        };
        window.clearTimeout = id => { timers.delete(id); stop(id); };
    })();""")
    portal.open()
    page.get_by_role("button", name="Generate preview", exact=True).click()
    expect(page.get_by_label("Operation review", exact=True)).to_contain_text(
        "running", timeout=10000
    )
    page.get_by_role("link", name="Customer 7", exact=True).click()
    expect(page.get_by_role("heading", name="EOSC", exact=True)).to_be_visible()
    polls = len(api.matching("GET", "/api/admin/gitops-operations/op-1"))
    page.wait_for_timeout(1400)
    assert len(api.matching("GET", "/api/admin/gitops-operations/op-1")) == polls
    assert page.evaluate("window.__pollTimers.size") == 0
    assert not api.matching("POST", GITOPS + "/publish")


def test_reader_acknowledgement_only_affects_selected_cluster(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.clusters["eosc-two"] = cluster_record("eosc-two")
    portal.open()
    page.get_by_label("Reader version installed on this cluster", exact=True).fill("3")
    page.once("dialog", lambda dialog: dialog.accept())
    page.get_by_role("button", name="Acknowledge reader installation", exact=True).click()
    expect(page.locator(".gitops-lifecycle").get_by_role("status")).to_contain_text(
        "acknowledged on eosc-one only"
    )
    assert api.matching("POST", GITOPS + "/reader-installed")[0].body == {"version": 3}
    assert api.draft("eosc-two")["reader_installed_version"] is None
    expect(page.locator(".repository-clusters li").filter(has_text="eosc-one")).to_contain_text(
        "Reader installed: 3"
    )
    expect(page.locator(".repository-clusters li").filter(has_text="eosc-two")).to_contain_text(
        "not acknowledged"
    )


def test_inflight_operation_poll_aborts_on_navigation(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    held_requests: list[Route] = []
    page.route("**/api/admin/gitops-operations/*", lambda route: held_requests.append(route))
    portal.open()
    with page.expect_request("**/api/admin/gitops-operations/op-1"):
        page.get_by_role("button", name="Generate preview", exact=True).click()
    with page.expect_event(
        "requestfailed", predicate=lambda request: "/gitops-operations/op-1" in request.url
    ):
        page.get_by_role("link", name="Customer 7", exact=True).click()
    expect(page.get_by_role("heading", name="EOSC", exact=True)).to_be_visible()
    assert len(held_requests) == 1
    assert not api.matching("POST", GITOPS + "/publish")


def test_failed_poll_stops_and_refresh_resumes_without_another_preview(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    operation_url = "/api/admin/gitops-operations/op-1"
    api.errors[("GET", operation_url)] = (
        503,
        {"detail": "Worker status unavailable", "request_id": "poll-request-9"},
    )
    portal.open()
    page.get_by_role("button", name="Generate preview", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("poll-request-9")
    expect(page.get_by_role("alert")).to_contain_text("Use Refresh status to resume")
    page.wait_for_timeout(1400)
    assert len(api.matching("GET", operation_url)) == 1
    page.get_by_role("button", name="Refresh status", exact=True).click()
    expect(page.get_by_label("GitOps diff", exact=True)).to_have_text(DIFF, timeout=10000)
    assert len(api.matching("POST", GITOPS + "/preview")) == 1
    assert not api.matching("POST", GITOPS + "/publish")


def test_version_conflict_preserves_edit_and_shows_correlation(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.errors[("PATCH", CLUSTER)] = (
        409,
        {
            "detail": "Configuration changed; reload before saving",
            "request_id": "config-request-8",
        },
    )
    portal.open()
    page.get_by_label("Portal display name", exact=True).fill("Operator edit")
    page.get_by_role("button", name="Save cluster settings", exact=True).click()
    expect(page.get_by_role("alert")).to_contain_text("config-request-8")
    expect(page.get_by_label("Portal display name", exact=True)).to_have_value("Operator edit")
    expect(page.get_by_role("button", name="Save cluster settings", exact=True)).to_be_enabled()
    assert len(api.matching("PATCH", CLUSTER)) == 1


def test_writer_commit_failure_recovery_reloads_and_requires_explicit_confirmation(
    portal: Portal,
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    credential_url = REPOSITORY + "/credentials/writer"
    api.secret_versions["writer"] = 3
    detail = {
        "code": "credential_commit_failed",
        "kind": "writer",
        "repository_version": 4,
        "pinned_secret_version": 2,
        "expected_secret_version": 2,
        "written_secret_version": 3,
        "message": "KV write succeeded but the database commit was not confirmed",
    }
    api.errors[("POST", credential_url)] = (
        503,
        {"detail": detail, "request_id": "commit-request-1"},
    )
    portal.open("/admin/customers/7")
    page.get_by_text("Edit shared repository", exact=True).click()
    token = page.get_by_label("New writer token", exact=True)
    replace = page.get_by_role("button", name="Replace writer credential", exact=True)
    token.fill("synthetic-first-token")
    replace.click()
    expect(page.get_by_role("alert")).to_contain_text("commit-request-1")
    metadata = page.get_by_label("Writer recovery version metadata", exact=True)
    expect(metadata).to_contain_text("credential_commit_failed")
    expect(metadata.locator(".row").filter(has_text="Confirmed KV write version")).to_contain_text(
        "3"
    )
    version = page.get_by_label("Confirmed writer secret version (advanced)", exact=True)
    expect(version).to_be_visible()
    expect(version).to_have_value("")
    expect(version).to_be_disabled()
    expect(token).to_have_value("")
    assert api.matching("POST", credential_url)[0].body == {
        "expected_version": 4,
        "username": "eosc-writer",
        "token": "[redacted]",
    }
    preserved = page.evaluate(
        "detail => apiError(new Response('', {status: 503}), {detail}).detail", detail
    )
    assert preserved == detail

    token.fill("synthetic-recovery-token")
    replace.click()
    expect(page.get_by_role("alert")).to_contain_text("Reload repository status")
    assert len(api.matching("POST", credential_url)) == 1
    api.repository["version"] = 9
    page.get_by_role("button", name="Reload writer repository status", exact=True).click()
    expect(metadata).to_contain_text("Repository status reloaded")
    expect(metadata.locator(".row").filter(has_text="Current repository version")).to_contain_text(
        "9"
    )
    expect(
        metadata.locator(".row").filter(has_text="Repository version at attempt")
    ).to_contain_text("4")
    expect(metadata).to_contain_text("commit-request-1")
    expect(version).to_be_enabled()
    expect(version).to_have_value("")
    expect(token).to_have_value("")
    version.fill("3")
    token.fill("synthetic-recovery-token")
    page.once("dialog", lambda dialog: dialog.dismiss())
    replace.click()
    assert len(api.matching("POST", credential_url)) == 1
    confirmations: list[str] = []
    page.once("dialog", lambda dialog: (confirmations.append(dialog.message), dialog.accept()))
    replace.click()
    assert "expected_secret_version=3" in confirmations[0]
    assert "customer 7 (test)" in confirmations[0]
    assert "synthetic-recovery-token" not in confirmations[0]
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Writer credential replaced"
    )
    assert api.matching("POST", credential_url)[1].body == {
        "expected_version": 9,
        "username": "eosc-writer",
        "token": "[redacted]",
        "expected_secret_version": 3,
    }
    assert api.repository["writer_secret_version"] == 4
    expect(token).to_have_value("")
    expect(version).to_have_value("")


@pytest.mark.parametrize("kind", ["writer", "reader"])
def test_cas_recovery_conflict_never_adopts_latest_or_retries_automatically(
    portal: Portal, kind: str
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.secret_versions[kind] = 6
    credential_url = REPOSITORY + f"/credentials/{kind}"
    portal.open("/admin/customers/7")
    page.get_by_text("Edit shared repository", exact=True).click()
    token = page.get_by_label(f"New {kind} token", exact=True)
    replace = page.get_by_role("button", name=f"Replace {kind} credential", exact=True)
    token.fill("synthetic-cas-token")
    replace.click()
    expect(page.get_by_role("alert")).to_contain_text("secret_version_conflict")
    metadata = page.get_by_label(f"{kind.title()} recovery version metadata", exact=True)
    expect(metadata).to_contain_text("cas-request-2")
    if kind == "writer":
        expect(
            metadata.locator(".row").filter(has_text="Observed writer KV version")
        ).to_contain_text("6")
    else:
        expect(metadata).to_contain_text("reader secret is never read")
        expect(metadata).not_to_contain_text("Observed writer KV version")
    version = page.get_by_label(f"Confirmed {kind} secret version (advanced)", exact=True)
    expect(version).to_have_value("")
    expect(version).to_be_disabled()
    page.get_by_role("button", name=f"Reload {kind} repository status", exact=True).click()
    expect(version).to_be_enabled()
    expect(version).to_have_value("")
    version.fill("4")
    token.fill("synthetic-cas-token")
    page.once("dialog", lambda dialog: dialog.accept())
    replace.click()
    expect(version).to_be_disabled()
    expect(version).to_have_value("")
    expect(token).to_have_value("")
    expect(metadata.locator(".row").filter(has_text="Attempted CAS version")).to_contain_text("4")
    page.wait_for_timeout(1200)
    attempts = api.matching("POST", credential_url)
    assert len(attempts) == 2
    assert "expected_secret_version" not in attempts[0].body
    assert attempts[1].body["expected_secret_version"] == 4
    assert api.secret_versions[kind] == 6


def test_explicit_zero_cas_recovery_and_blank_token_preservation(portal: Portal) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.repository["reader_secret_version"] = None
    credential_url = REPOSITORY + "/credentials/reader"
    api.errors[("POST", credential_url)] = (
        409,
        {
            "detail": {
                "code": "secret_version_confirmation_required",
                "kind": "reader",
                "repository_version": 4,
                "pinned_secret_version": None,
                "expected_secret_version": None,
                "latest_secret_version": None,
            }
        },
    )
    portal.open("/admin/customers/7")
    page.get_by_text("Edit shared repository", exact=True).click()
    token = page.get_by_label("New reader token", exact=True)
    replace = page.get_by_role("button", name="Replace reader credential", exact=True)
    token.fill("synthetic-reader-token")
    replace.click()
    expect(page.get_by_role("alert")).to_contain_text("secret_version_confirmation_required")
    page.get_by_role("button", name="Reload reader repository status", exact=True).click()
    version = page.get_by_label("Confirmed reader secret version (advanced)", exact=True)
    expect(version).to_be_enabled()
    version.fill("0")
    replace.click()
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Reader credential kept"
    )
    assert len(api.matching("POST", credential_url)) == 1
    assert api.repository["reader_secret_version"] is None
    token.fill("synthetic-reader-token")
    version.fill("-1")
    replace.click()
    assert len(api.matching("POST", credential_url)) == 1
    version.fill("0.5")
    replace.click()
    assert len(api.matching("POST", credential_url)) == 1
    version.fill("0")
    page.once("dialog", lambda dialog: dialog.accept())
    replace.click()
    expect(page.locator(".repository-editor").get_by_role("status")).to_contain_text(
        "Reader credential replaced"
    )
    assert api.matching("POST", credential_url)[1].body["expected_secret_version"] == 0
    assert api.repository["reader_secret_version"] == 1
    expect(token).to_have_value("")


@pytest.mark.parametrize("diff,head", [(DIFF, EXPECTED_HEAD), ("", EXPECTED_HEAD), (DIFF, None)])
def test_prepared_preview_shows_actual_revisions_source_and_validation(
    portal: Portal, diff: str, head: str | None
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.hold_operations = True
    api.preview_diff = diff
    api.preview_head = head
    portal.open()
    repository = page.locator(".repository-editor")
    expect(repository).to_contain_text("Approved default bases revision")
    expect(repository).to_contain_text(DEFAULT_BASES)
    expect(repository).to_contain_text(
        "An existing repository gitlink may pin a different revision"
    )
    page.get_by_role("button", name="Generate preview", exact=True).click()
    review = page.get_by_label("Operation review", exact=True)
    expect(review).to_contain_text("The diff will appear when preview generation completes")
    expect(review.get_by_label("GitOps diff", exact=True)).to_have_count(0)
    expect(review).not_to_contain_text("The preview contains no file changes")
    expect(review).not_to_contain_text("Empty repository (no HEAD)")
    expect(review.get_by_label("Preview source snapshot", exact=True)).to_have_count(0)
    assert api.operations["op-1"]["diff"] is None

    api.hold_operations = False
    rendered_diff = review.get_by_label("GitOps diff", exact=True)
    expect(rendered_diff).to_be_visible(timeout=10000)
    expect(rendered_diff).to_have_text(diff)
    expect(review.locator(".row").filter(has_text="Preview bases revision")).to_contain_text(
        PREVIEW_BASES
    )
    expect(review).not_to_contain_text(DEFAULT_BASES)
    expect(review.locator(".row").filter(has_text="Expected repository HEAD")).to_contain_text(
        head or "Empty repository (no HEAD)"
    )
    expected = prepared_metadata("eosc-one")
    source = review.get_by_label("Preview source snapshot", exact=True)
    validation = review.get_by_label("Preview validation metadata", exact=True)
    assert json.loads(source.inner_text()) == expected["source"]
    assert json.loads(validation.inner_text()) == expected["validation"]
    if diff == "":
        expect(review).to_contain_text("The preview contains no file changes")
    else:
        expect(review).not_to_contain_text("The preview contains no file changes")
    expect(page.get_by_role("button", name="Publish reviewed diff", exact=True)).to_be_enabled()

    api.infrastructure["inventory_commit"] = "d" * 40
    page.get_by_role("button", name="Refresh status", exact=True).click()
    expect(page.get_by_label("Lifecycle status", exact=True)).to_contain_text("d" * 40)
    expect(review.locator(".row").filter(has_text="Source inventory commit")).to_contain_text(
        SOURCE_INVENTORY_COMMIT
    )
    assert json.loads(source.inner_text())["inventory_commit"] == SOURCE_INVENTORY_COMMIT
    api.operations["op-1"]["validation"]["kustomizations"].append("<img src=x onerror=alert(1)>")
    page.reload()
    expect(validation).to_contain_text("<img src=x onerror=alert(1)>")
    expect(page.locator(".gitops-metadata img")).to_have_count(0)
    assert not api.matching("POST", GITOPS + "/publish")


def test_lost_publish_response_retry_returns_succeeded_and_refreshes_without_polling(
    portal: Portal,
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    portal.open()
    page.get_by_role("button", name="Generate preview", exact=True).click()
    expect(page.get_by_label("GitOps diff", exact=True)).to_have_text(DIFF, timeout=10000)
    api.lose_publish_response = True
    page.on("dialog", lambda dialog: dialog.accept())
    publish = page.get_by_role("button", name="Publish reviewed diff", exact=True)
    publish.click()
    expect(page.get_by_role("alert")).to_contain_text("Failed to fetch")
    assert api.operations["op-1"]["status"] == "succeeded"
    expect(publish).to_be_enabled()
    polls_before_retry = len(api.matching("GET", "/api/admin/gitops-operations/op-1"))
    reads_before_retry = len(api.matching("GET", GITOPS))

    publish.click()
    expect(page.locator(".gitops-lifecycle").get_by_role("status")).to_contain_text(
        "Publication succeeded. Commit: published-456."
    )
    expect(page.get_by_label("Lifecycle status", exact=True)).to_contain_text("published-456")
    expect(page.get_by_label("Operation review", exact=True)).to_contain_text("succeeded")
    expect(publish).to_be_disabled()
    expect(page.locator(".gitops-lifecycle .workflow-progress")).to_have_count(0)
    assert len(api.matching("GET", GITOPS)) == reads_before_retry + 1
    page.wait_for_timeout(1400)
    assert len(api.matching("GET", "/api/admin/gitops-operations/op-1")) == polls_before_retry
    assert [call.body for call in api.matching("POST", GITOPS + "/publish")] == [
        {"operation_id": "op-1"},
        {"operation_id": "op-1"},
    ]


@pytest.mark.parametrize(
    "source_status,badge_kind,hint",
    [
        ("ready", "ready", None),
        ("pending", "pending", None),
        ("failed", "error", "Resolve the reported operator error"),
        ("suspended", "error", "Resume operator reconciliation"),
    ],
)
def test_infrastructure_status_controls_readiness_with_real_backend_phase(
    portal: Portal, source_status: str, badge_kind: str, hint: str | None
) -> None:
    page, api = portal.page, portal.api
    api.ready()
    api.infrastructure.update(
        status=source_status,
        phase="VirtualMachinesReady",
        message=f"Operator status: {source_status}",
    )
    portal.open()
    lifecycle = page.get_by_label("Lifecycle status", exact=True)
    vm_status = lifecycle.locator(".row").filter(has_text="VM infrastructure")
    expect(vm_status).to_contain_text("VirtualMachinesReady")
    expect(vm_status.locator(".badge")).to_have_attribute("class", f"badge {badge_kind}")
    preview = page.get_by_role("button", name="Generate preview", exact=True)
    if source_status == "ready":
        expect(preview).to_be_enabled()
    else:
        expect(preview).to_be_disabled()
    if hint:
        expect(lifecycle.locator(".hint.err")).to_contain_text(hint)
    else:
        expect(lifecycle.locator(".hint.err")).to_have_count(0)
