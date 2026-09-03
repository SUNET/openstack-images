"""Customer Portal API — FastAPI application."""

import hashlib
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import git
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from kubernetes.client.rest import ApiException as K8sApiException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.auth import get_current_user, get_user_contracts, init_oauth, oauth
from app.cluster_client import TenantClusterError
from app.cluster_git_backend import ClusterGitBackend
from app.config import get_settings
from app.crypto import init_crypto
from app.db import close_db, get_session, init_db, run_migrations
from app.git_backend import GitBackend
from app.k8s import init_k8s
from app.openbao_client import OpenBaoError, init_openbao, shutdown_openbao
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

    # Existing deployments can roll out the application before adding the
    # second repository credential. Cluster planning remains unavailable
    # until it is configured.
    app.state.cluster_git_backend = None
    if settings.cluster_git_repo_url:
        cluster_git_backend = ClusterGitBackend(settings)
        cluster_git_backend.init()
        app.state.cluster_git_backend = cluster_git_backend
        logger.info("Cluster git backend initialized")
    else:
        logger.warning(
            "CLUSTER_GIT_REPO_URL is not configured; cluster planning is disabled"
        )

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


app = FastAPI(title="Customer Portal API", version="0.1.13", lifespan=lifespan)

_settings = get_settings()
_BASE_ORIGIN = (
    f"{urlparse(_settings.base_url).scheme}://{urlparse(_settings.base_url).netloc}"
)
# Mutating verbs that require Origin/Referer enforcement.
_MUTATING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
# Routes that do NOT use cookie-based session auth and so don't need CSRF
# protection. Adding to this list is sensitive — only paths that authenticate
# strictly with a non-cookie mechanism (bearer token, mTLS) belong here.
_CSRF_EXEMPT_PATHS = {
    "/api/billing/run-due",  # bearer-token only (constant-time compared)
    "/callback",  # OIDC callback completes the login redirect
}


class OriginEnforcementMiddleware(BaseHTTPMiddleware):
    """Reject mutating requests whose Origin/Referer disagree with BASE_URL.

    SameSite=lax already blocks cross-site cookies on most mutating verbs in
    modern browsers, but defence-in-depth: explicitly require that the
    request originates from our own front-end. State-changing tools that
    set Origin (fetch/XHR) get checked first; older form-style submits fall
    back to Referer.
    """

    async def dispatch(self, request: Request, call_next):
        if request.method in _MUTATING_METHODS and request.url.path not in _CSRF_EXEMPT_PATHS:
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            if origin:
                if origin != _BASE_ORIGIN:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Cross-origin request refused"},
                    )
            elif referer:
                ref = urlparse(referer)
                if f"{ref.scheme}://{ref.netloc}" != _BASE_ORIGIN:
                    return JSONResponse(
                        status_code=403,
                        content={"detail": "Cross-origin request refused"},
                    )
            else:
                return JSONResponse(
                    status_code=403,
                    content={"detail": "Origin or Referer header required"},
                )
        return await call_next(request)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply baseline security response headers."""

    # CSP: same-origin scripts/styles/connects/fonts/images, no inline,
    # no plugins, no framing. Adjust if a future feature needs CDN assets.
    _CSP = (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "object-src 'none'"
    )

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("Content-Security-Policy", self._CSP)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response


# Order matters: outermost first. CORS → SessionMiddleware → CSRF → security
# headers → routes. Starlette adds in reverse, so we add them in this order:
app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(OriginEnforcementMiddleware)
app.add_middleware(
    SessionMiddleware,
    secret_key=_settings.secret_key,
    session_cookie=_settings.session_cookie_name,
    max_age=_settings.session_max_age_seconds,
    same_site="lax",
    https_only=_settings.session_https_only,
)
# CORS is restrictive: only the configured BASE_URL origin, with credentials.
app.add_middleware(
    CORSMiddleware,
    allow_origins=[_BASE_ORIGIN],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization"],
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


def _correlation_id() -> str:
    return uuid.uuid4().hex[:12]


@app.exception_handler(ValueError)
async def value_error_handler(request: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


@app.exception_handler(git.GitCommandError)
async def git_error_handler(request: Request, exc: git.GitCommandError):
    cid = _correlation_id()
    logger.error("Git operation failed [cid=%s]: %s", cid, exc)
    return JSONResponse(
        status_code=503,
        content={"detail": "Git operation failed, please try again", "request_id": cid},
    )


@app.exception_handler(OpenBaoError)
async def openbao_error_handler(request: Request, exc: OpenBaoError):
    cid = _correlation_id()
    logger.error("OpenBao error [cid=%s]: %s", cid, exc)
    return JSONResponse(
        status_code=502,
        content={
            "detail": "Tenant credential service is unavailable",
            "request_id": cid,
        },
    )


@app.exception_handler(TenantClusterError)
async def tenant_cluster_error_handler(request: Request, exc: TenantClusterError):
    cid = _correlation_id()
    logger.error("Tenant cluster error [cid=%s]: %s", cid, exc)
    return JSONResponse(
        status_code=502,
        content={
            "detail": "Tenant cluster operation failed",
            "request_id": cid,
        },
    )


@app.exception_handler(K8sApiException)
async def k8s_api_error_handler(request: Request, exc: K8sApiException):
    cid = _correlation_id()
    body = exc.body if isinstance(exc.body, str) else "(no body)"
    logger.error(
        "Tenant K8s API error [cid=%s]: status=%s reason=%s body=%s",
        cid, exc.status, exc.reason, body[:500],
    )
    return JSONResponse(
        status_code=502,
        content={
            "detail": f"Tenant K8s API error (status {exc.status})",
            "request_id": cid,
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
