"""Customer Portal API — FastAPI application."""

import hashlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import git
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from kubernetes.client.rest import ApiException as K8sApiException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from app.cluster_client import TenantClusterError
from app.openbao_client import OpenBaoError

from app.auth import get_current_user, get_user_contracts, init_oauth, oauth
from app.config import get_settings
from app.crypto import init_crypto
from app.db import close_db, get_session, init_db, run_migrations
from app.git_backend import GitBackend
from app.k8s import init_k8s
from app.openbao_client import init_openbao, shutdown_openbao
from app.routers import admin, billing, cluster_requests, clusters, kubeconfig, projects
from app.schemas import ContractWithCustomerResponse, UserInfo

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    # Initialize database and run migrations
    init_db(settings.database_url)
    run_migrations(settings.database_url)
    logger.info("Database initialized")

    # Initialize git backend
    git_backend = GitBackend(settings)
    git_backend.init()
    app.state.git_backend = git_backend
    logger.info("Git backend initialized")

    # Initialize Kubernetes client
    try:
        init_k8s()
        logger.info("Kubernetes client initialized")
    except Exception:
        logger.warning("Kubernetes client not available (running outside cluster?)")

    # Initialize crypto for credential encryption
    init_crypto(settings.secret_key)

    # Initialize OpenBao client (used by tenant cluster operations)
    init_openbao(settings)
    logger.info("OpenBao client configured (%s)", settings.openbao_addr)

    # Initialize OIDC
    init_oauth(settings)
    logger.info("OIDC provider configured")

    yield

    await shutdown_openbao()
    await close_db()


app = FastAPI(title="Customer Portal API", version="0.1.0", lifespan=lifespan)

# Session middleware for OIDC auth
_settings = get_settings()
app.add_middleware(SessionMiddleware, secret_key=_settings.secret_key)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_settings.base_url] if _settings.base_url else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Include routers
app.include_router(admin.router)
app.include_router(projects.router)
app.include_router(billing.router)
app.include_router(clusters.admin_router)
app.include_router(clusters.member_router)
app.include_router(cluster_requests.admin_router)
app.include_router(cluster_requests.member_router)
app.include_router(kubeconfig.router)


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(git.GitCommandError)
async def git_error_handler(request: Request, exc: git.GitCommandError):
    logger.error("Git operation failed: %s", exc)
    return JSONResponse(status_code=503, content={"detail": "Git operation failed, please try again"})


@app.exception_handler(OpenBaoError)
async def openbao_error_handler(request: Request, exc: OpenBaoError):
    logger.error("OpenBao error: %s", exc)
    return JSONResponse(
        status_code=502,
        content={"detail": f"OpenBao error: {exc}. Check that the per-tenant secrets engine "
                           "and policy are configured (see deployment runbook)."},
    )


@app.exception_handler(TenantClusterError)
async def tenant_cluster_error_handler(request: Request, exc: TenantClusterError):
    logger.error("Tenant cluster error: %s", exc)
    return JSONResponse(
        status_code=502, content={"detail": f"Tenant cluster error: {exc}"}
    )


@app.exception_handler(K8sApiException)
async def k8s_api_error_handler(request: Request, exc: K8sApiException):
    logger.error("Tenant K8s API error: %s %s", exc.status, exc.reason)
    body = exc.body if isinstance(exc.body, str) else "(no body)"
    return JSONResponse(
        status_code=502,
        content={
            "detail": f"Tenant K8s API {exc.status} {exc.reason}: {body[:300]}"
        },
    )


@app.get("/healthz")
async def healthz():
    return {"status": "ok"}


@app.get("/auth/login")
async def login(request: Request):
    redirect_uri = _settings.oidc_redirect_uri
    return await oauth.oidc.authorize_redirect(request, redirect_uri)


@app.get("/callback")
async def callback(request: Request):
    token = await oauth.oidc.authorize_access_token(request)
    userinfo = token.get("userinfo", {})
    request.session["user"] = {
        "sub": userinfo.get("sub", ""),
        "name": userinfo.get("name", ""),
        "email": userinfo.get("email", ""),
    }
    return RedirectResponse(url="/")


@app.get("/auth/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/")


@app.get("/api/me", response_model=UserInfo)
async def me(
    user: dict[str, Any] = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
):
    contracts = await get_user_contracts(user["sub"], session)
    return UserInfo(
        sub=user["sub"],
        name=user.get("name"),
        email=user.get("email"),
        is_admin=user["sub"] in _settings.admin_users,
        contracts=[ContractWithCustomerResponse.model_validate(c) for c in contracts],
    )


# Serve static frontend with content-hash cache-busting on the SPA bundle.
# index.html is rendered once at startup with `?v=<hash>` appended to the
# `/app.js` and `/style.css` URLs so browsers fetch fresh bundles after a
# redeploy without users having to hard-reload.
_static_dir = Path(__file__).parent.parent / "static"


def _render_index(static_dir: Path) -> str:
    index = (static_dir / "index.html").read_text()
    bundle_parts: list[bytes] = []
    for asset in ("app.js", "style.css"):
        path = static_dir / asset
        if path.is_file():
            bundle_parts.append(path.read_bytes())
    digest = hashlib.sha256(b"".join(bundle_parts)).hexdigest()[:12]
    index = (
        index
        .replace('href="/style.css"', f'href="/style.css?v={digest}"')
        .replace('src="/app.js"', f'src="/app.js?v={digest}"')
    )
    # In test, recolour the UI and flag the title/brand so it's obvious which
    # environment you're looking at. Gated by PORTAL_IS_IN_TEST.
    if get_settings().is_test:
        index = (
            index
            .replace(
                "<title>SUNET Cloud Portal</title>",
                "<title>[TEST] SUNET Cloud Portal</title>",
            )
            .replace("<body>", '<body class="test-env">')
            .replace(
                "<span>SUNET&nbsp;Cloud</span>",
                '<span>SUNET&nbsp;Cloud</span>'
                '<span class="env-badge">TEST</span>',
            )
        )
    return index


if _static_dir.is_dir():
    _index_html = _render_index(_static_dir)

    @app.get("/", response_class=HTMLResponse)
    async def index() -> str:
        return _index_html

    # Mounted *after* the explicit `/` route so it serves the rest of the
    # asset paths (`/app.js`, `/style.css`, …) without shadowing index.
    app.mount("/", StaticFiles(directory=_static_dir, html=False), name="static")
