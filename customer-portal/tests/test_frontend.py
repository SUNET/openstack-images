"""Static contracts for the portal shell, selectors, and generated markup."""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

PORTAL_ROOT = Path(__file__).parents[1]
APP_JS = PORTAL_ROOT / "static" / "app.js"
INDEX_HTML = PORTAL_ROOT / "static" / "index.html"
STYLE_CSS = PORTAL_ROOT / "static" / "style.css"


@pytest.mark.asyncio
async def test_root_and_index_html_serve_processed_shell() -> None:
    from app.main import app

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        root = await client.get("/")
        index = await client.get("/index.html")
        logo = await client.get("/sunet-logo.svg")

    assert root.status_code == 200
    assert index.status_code == 200
    assert logo.status_code == 200
    assert logo.headers["content-type"].startswith("image/svg+xml")
    assert root.text == index.text
    assert re.search(r'href="/style\.css\?v=[0-9a-f]{12}"', root.text)
    assert re.search(r'src="/app\.js\?v=[0-9a-f]{12}"', root.text)
    assert re.search(r'src="/sunet-logo\.svg\?v=[0-9a-f]{12}"', root.text)
    assert 'href="https://sunetvdc.se/"' in root.text


def test_mobile_navigation_has_synchronised_accessibility_hooks() -> None:
    html = INDEX_HTML.read_text()
    js = APP_JS.read_text()

    assert 'aria-controls="nav"' in html
    assert 'aria-expanded="false"' in html
    assert 'toggle.setAttribute("aria-expanded", String(open))' in js
    assert '"aria-current": l.key === active ? "page" : null' in js


def test_literal_labels_have_matching_controls() -> None:
    js = APP_JS.read_text()
    label_targets = set(re.findall(r'htmlFor: "([^"]+)"', js))
    control_ids = set(re.findall(r'id: "([^"]+)"', js))

    assert label_targets
    assert label_targets <= control_ids


def test_selector_contracts_use_the_intended_hooks() -> None:
    css = STYLE_CSS.read_text()
    js = APP_JS.read_text()

    assert "a.text-link {" in css
    assert 'className: "dz-row-name text-link"' in js
    assert 'className: "link"' not in js
    assert ".btn:disabled" in css
    assert ".danger-zone .slbl + .dz-row" in css
    assert ".danger-zone .dz-row:first-of-type" not in css
    assert 'app.querySelector(".flash-alert")' in js


def test_kubeconfig_uses_a_native_dialog_with_textarea_content() -> None:
    js = APP_JS.read_text()

    assert 'h("dialog", {' in js
    assert 'className: "portal-dialog"' in js
    assert "}, issued.kubeconfig)," in js
    assert "value: issued.kubeconfig" not in js
    assert "dialog.showModal()" in js
    assert js.count("route({ preserveDialogs: true })") == 2


def test_multisection_workflows_use_one_submit_form() -> None:
    js = APP_JS.read_text()

    for section in ("identity", "access", "basics", "scope", "schedule", "period"):
        assert f'const {section} = h("form"' not in js
    assert 'const manualForm = h("form", {' in js
    assert js.count('const workflow = h("form", { onsubmit:') >= 3


def test_create_project_explains_automatic_domain_qualification() -> None:
    js = APP_JS.read_text()

    assert "Enter only the project name — do not append a domain." in js
    assert "The customer domain is added automatically." in js
    assert "my-project becomes my-project.${customerDomain}" in js
    assert 'const errorPrefix = "Value error, ";' in js


def test_documented_kubectl_command_requires_an_explicit_context() -> None:
    js = APP_JS.read_text()

    assert "Set and pass the intended Kubernetes context explicitly" in js
    assert "context='<admin-context>'" in js
    assert '  --context \\"$context\\" apply' in js


def test_route_requests_abort_obsolete_renderers() -> None:
    js = APP_JS.read_text()

    assert "routeAbortController?.abort()" in js
    assert "const signal = opts.signal || routeAbortController?.signal" in js
    assert "await suspendAbortedRoute(error, signal)" in js
    for renderer in (
        "renderAdminContractDetail",
        "renderAdminPricing",
        "renderClusterDetail",
        "renderClusterUsers",
        "renderAdminClusterDetail",
        "renderAdminClusterRequests",
    ):
        assert len(re.findall(rf"\b{renderer}\(", js)) == 2


def test_gitops_uses_text_only_diffs_and_cancellable_polling() -> None:
    js = APP_JS.read_text(encoding="utf-8")

    assert 'diff.textContent = typeof operation.diff === "string"' in js
    assert 'className: "gitops-diff"' in js
    assert "bootstrap-gitops" not in js
    assert "setInterval(" not in js
    assert "clearTimeout(timer)" in js
    assert 'signal.removeEventListener("abort", abort)' in js
    assert "await waitForPoll(controller.signal)" in js
    assert "request_id: ${requestId}" in js
    assert "error.detail = body.detail" in js


def test_cluster_creation_has_no_repository_credential_fields() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    create = js.split("async function renderAdminCreateCluster()", 1)[1].split(
        "function clusterSettingsEditor", 1
    )[0]

    assert "Create and start provisioning" in create
    assert "repositoryReady(repository)" in create
    assert "customer_repository_" not in create
    assert 'type: "password"' not in create
    assert "localStorage" not in js
    assert "sessionStorage" not in js


def test_live_cluster_keeps_versioned_settings_and_shared_repository_editor() -> None:
    js = APP_JS.read_text(encoding="utf-8")
    detail = js.split("async function renderAdminClusterDetail(slug)", 1)[1].split(
        "async function renderAdminClusterRequests", 1
    )[0]

    assert "sharedRepositoryEditor(c.customer_id" in detail
    assert "clusterSettingsEditor(c," in detail
    assert "if (!c.provisioned_at)" not in detail
    assert "config_version: cluster.config_version" in js
    assert "expected_version: repository.version" in js
    assert "Advanced RBAC (read-only)" in js
