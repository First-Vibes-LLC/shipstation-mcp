"""
Reusable MCP OAuth 2.0 support for FastAPI-based MCP servers.

Provides discovery (RFC 8414), dynamic client registration, authorization code
flow with PKCE (RFC 7636), and token exchange. Use init_oauth() to mount
routes and verify_token (or verify_token_except_initialize for MCP POST) as FastAPI Depends().

When behind a reverse proxy that rewrites to /mcp{path}, set env MCP_MOUNT_PATH=/mcp
and run uvicorn with get_asgi_app(app) so the app is served under /mcp.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

logger = logging.getLogger(__name__)

router = APIRouter()
security = HTTPBearer(auto_error=False)


def _extract_bearer_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
) -> str | None:
    """
    Resolve bearer token from HTTPBearer, raw Authorization header, or X-Access-Token.
    Some proxies/clients omit the Bearer prefix or use alternate headers.
    """
    if credentials and credentials.credentials:
        t = credentials.credentials.strip()
        if t:
            return t

    auth = request.headers.get("authorization") or request.headers.get(
        "Authorization"
    )
    if auth:
        auth = auth.strip()
        low = auth.lower()
        if low.startswith("basic "):
            return None
        if low.startswith("bearer "):
            rest = auth[7:].strip()
            return rest or None
        # Single token without scheme (some clients/proxies strip "Bearer ")
        if " " not in auth:
            return auth

    xat = request.headers.get("x-access-token") or request.headers.get(
        "X-Access-Token"
    )
    if xat:
        return xat.strip()

    # Some reverse proxies forward the original Authorization under a different name
    # (or clients send Bearer here when the front hop strips Authorization).
    for hdr in (
        "x-forwarded-authorization",
        "X-Forwarded-Authorization",
        "x-original-authorization",
        "X-Original-Authorization",
    ):
        fwd = request.headers.get(hdr)
        if not fwd:
            continue
        fwd = fwd.strip()
        low = fwd.lower()
        if low.startswith("basic "):
            continue
        if low.startswith("bearer "):
            rest = fwd[7:].strip()
            if rest:
                return rest
        if " " not in fwd:
            return fwd

    return None


# In-memory stores — auth codes / clients are per-process; access tokens are
# HMAC-signed (see OAUTH_ACCESS_TOKEN_SECRET) so multiple workers/replicas work.
_auth_codes: dict[str, dict] = {}
_access_tokens: dict[str, dict] = {}  # legacy random tokens only (if any)
_registered_clients: dict[str, dict] = {}
_static_tokens: set = set()


def _oauth_signing_secret() -> bytes:
    """Secret for stateless access tokens. Set OAUTH_ACCESS_TOKEN_SECRET in production."""
    key = os.environ.get("OAUTH_ACCESS_TOKEN_SECRET", "").strip()
    if key:
        return key.encode("utf-8")
    return b"mcp-oauth-dev-insecure-change-in-production"


def _mint_signed_access_token(client_id: str) -> str:
    exp = int(time.time()) + 3600
    msg = f"{exp}|{client_id}".encode()
    sig = hmac.new(_oauth_signing_secret(), msg, hashlib.sha256).hexdigest()
    raw = f"{exp}|{client_id}|{sig}".encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _verify_signed_access_token(token: str) -> bool:
    try:
        pad = "=" * ((4 - len(token) % 4) % 4)
        raw = base64.urlsafe_b64decode(token + pad).decode("utf-8")
        exp_s, client_id, sig = raw.rsplit("|", 2)
        exp = int(exp_s)
        if time.time() > exp:
            return False
        msg = f"{exp}|{client_id}".encode()
        expected = hmac.new(
            _oauth_signing_secret(), msg, hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(expected, sig)
    except Exception:
        return False


def _pkce_verify(code_verifier: str, code_challenge: str, method: str) -> bool:
    """
    Verify PKCE code_verifier against stored code_challenge.
    Supports S256 (required) and plain (dev only).
    """
    if method == "S256":
        digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
        computed = (
            base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
        )
        return computed == code_challenge
    if method == "plain":
        return code_verifier == code_challenge
    return False


def get_asgi_app(app, mount_path: str = None):
    """
    Return the ASGI app to run. If mount_path is set (e.g. /mcp), returns a root
    app with your app mounted at that path for reverse proxies that rewrite to
    /mcp{path}. Otherwise returns app unchanged.

    Usage: uvicorn.run(get_asgi_app(app), host="...", port=...)
    Reads MCP_MOUNT_PATH from env when mount_path is not provided.
    """
    path = (mount_path or os.environ.get("MCP_MOUNT_PATH", "")).rstrip("/")
    if not path:
        return app
    root = FastAPI(title="MCP (root)", redirect_slashes=False)
    root.mount(path, app)

    @root.get("/health")
    async def root_health():
        """Health at root so load balancers can GET /health when app is mounted at /mcp."""
        return {"status": "ok"}

    return root


def init_oauth(app, base_url: str, static_token: str = None):
    """
    Attach OAuth endpoints to a FastAPI app.

    Args:
        app: Your FastAPI instance
        base_url: Public base URL of your server (e.g. https://mcp.yourco.com)
        static_token: Optional legacy static bearer token (backwards compat with MCP_AUTH_TOKEN)
    """
    app.state.base_url = base_url.rstrip("/")
    app.state.static_token = static_token
    if static_token:
        _static_tokens.add(static_token)
    app.include_router(router)


def mcp_www_authenticate_headers(
    request: Request, *, error: str | None = None
) -> dict:
    """
    MCP authorization + RFC 9728: protected-resource 401s SHOULD include
    resource_metadata and scope in WWW-Authenticate so clients attach Bearer
    tokens to the correct resource (see MCP spec, Basic Authorization).
    """
    base = getattr(request.app.state, "base_url", None)
    base = str(base).rstrip("/") if base else str(request.base_url).rstrip("/")
    rm = f"{base}/.well-known/oauth-protected-resource"
    if error:
        val = f'Bearer error="{error}", resource_metadata="{rm}", scope="mcp:tools"'
    else:
        val = f'Bearer resource_metadata="{rm}", scope="mcp:tools"'
    return {"WWW-Authenticate": val}


def negotiate_mcp_protocol_version(params: dict | None) -> str:
    """
    Echo a supported MCP protocol version from the client's initialize params.
    Clients sending e.g. 2025-11-25 expect a matching negotiated version in InitializeResult.
    """
    req = (params or {}).get("protocolVersion") or "2024-11-05"
    supported = ("2025-11-25", "2025-03-26", "2024-11-05")
    if req in supported:
        return req
    return "2024-11-05"


# JSON-RPC methods allowed without Bearer on POST / or POST /mcp.
# - lifecycle: initialize, notifications/initialized
# - tools/list: Claude (browser) often omits Authorization on the same POST / session as OAuth
#   discovery even though tools/call still requires a valid token — without this, UI shows no tools.
_MCP_POST_UNAUTH_METHODS = frozenset(
    {
        "initialize",
        "notifications/initialized",
        "tools/list",
    }
)


async def _verify_token_core(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None,
):
    """Validate Bearer / static / signed OAuth token; raise HTTPException on failure."""
    token = _extract_bearer_token(request, credentials)

    if not token:
        if os.environ.get("OAUTH_DEBUG"):
            logger.warning(
                "oauth 401 missing bearer path=%s has_authorization_header=%s has_forwarded_auth=%s",
                request.url.path,
                bool(
                    request.headers.get("authorization")
                    or request.headers.get("Authorization")
                ),
                bool(
                    request.headers.get("x-forwarded-authorization")
                    or request.headers.get("X-Forwarded-Authorization")
                ),
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authorization required",
            headers=mcp_www_authenticate_headers(request),
        )

    # Legacy static token (MCP_AUTH_TOKEN)
    if token in _static_tokens:
        return True

    # OAuth access token (in-memory legacy)
    data = _access_tokens.get(token)
    if data and time.time() < data["expires_at"]:
        return True

    # Stateless signed token (works across uvicorn workers / replicas)
    if _verify_signed_access_token(token):
        return True

    if os.environ.get("OAUTH_DEBUG"):
        logger.warning(
            "oauth 401 invalid token path=%s token_len=%s starts_with=%s",
            request.url.path,
            len(token),
            token[:12] if len(token) >= 12 else token,
        )

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or expired token",
        headers=mcp_www_authenticate_headers(request, error="invalid_token"),
    )


async def verify_token(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),  # noqa: B008
):
    """
    FastAPI dependency: accept static token or valid OAuth access token.
    Use as Depends(verify_token) on protected routes.
    """
    return await _verify_token_core(request, credentials)


async def verify_token_except_initialize(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),  # noqa: B008
):
    """
    For MCP HTTP POST: allow unauthenticated JSON-RPC for MCP handshake and ``tools/list``.
    ``tools/call`` and other methods still require Bearer (OAuth or MCP_AUTH_TOKEN).

    Caches the request body on ``request.state._mcp_post_body_bytes`` — the handler must
    parse JSON from that instead of calling ``request.json()`` again.
    """
    body_bytes = await request.body()
    request.state._mcp_post_body_bytes = body_bytes
    try:
        msg = json.loads(body_bytes.decode("utf-8"))
    except Exception:
        msg = {}
    if msg.get("method") in _MCP_POST_UNAUTH_METHODS:
        return True
    return await _verify_token_core(request, credentials)


# ---------------------------------------------------------------------------
# OAuth Discovery (RFC 8414)
# ---------------------------------------------------------------------------


@router.get("/.well-known/oauth-authorization-server")
async def oauth_metadata(request: Request):
    """OAuth 2.0 Authorization Server Metadata for client discovery."""
    base = getattr(request.app.state, "base_url", None) or str(
        request.base_url
    ).rstrip("/")
    return {
        "issuer": base,
        "authorization_endpoint": f"{base}/oauth/authorize",
        "token_endpoint": f"{base}/oauth/token",
        "registration_endpoint": f"{base}/oauth/register",
        "scopes_supported": ["mcp:tools"],
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
    }


@router.get("/.well-known/oauth-protected-resource")
async def oauth_protected_resource_metadata(request: Request):
    """
    OAuth 2.0 Protected Resource Metadata.
    Some clients (including Claude connectors) probe this endpoint before
    attaching bearer tokens to protected resource requests.
    """
    base = getattr(request.app.state, "base_url", None) or str(
        request.base_url
    ).rstrip("/")
    return {
        "resource": base,
        "authorization_servers": [base],
        "bearer_methods_supported": ["header"],
        "scopes_supported": ["mcp:tools"],
    }


# ---------------------------------------------------------------------------
# Dynamic Client Registration (RFC 7591)
# ---------------------------------------------------------------------------


@router.post("/oauth/register")
async def register_client(request: Request):
    """Register a new OAuth client; returns client_id and client_secret."""
    body = await request.json()
    redirect_uris = body.get("redirect_uris") or []
    if isinstance(redirect_uris, str):
        redirect_uris = [redirect_uris]

    client_id = secrets.token_urlsafe(16)
    client_secret = secrets.token_urlsafe(32)

    _registered_clients[client_id] = {
        "client_secret": client_secret,
        "redirect_uris": redirect_uris,
        "client_name": body.get("client_name", "Unknown"),
    }

    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "redirect_uris": _registered_clients[client_id]["redirect_uris"],
    }


# ---------------------------------------------------------------------------
# Authorization (code with optional PKCE)
# ---------------------------------------------------------------------------


@router.get("/oauth/authorize")
async def authorize(
    response_type: str,
    client_id: str,
    redirect_uri: str,
    state: str = "",
    code_challenge: str = "",
    code_challenge_method: str = "S256",
):
    """
    Authorization endpoint: validate client and redirect_uri, then redirect
    back to client with a one-time authorization code.
    """
    if client_id not in _registered_clients:
        raise HTTPException(status_code=400, detail="Unknown client_id")

    client = _registered_clients[client_id]
    if redirect_uri not in client["redirect_uris"]:
        raise HTTPException(
            status_code=400, detail="redirect_uri not allowed for this client"
        )

    code = secrets.token_urlsafe(32)
    _auth_codes[code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": code_challenge,
        "code_challenge_method": code_challenge_method or "S256",
        "expires_at": time.time() + 600,
    }

    return RedirectResponse(
        url=f"{redirect_uri}?{urlencode({'code': code, 'state': state})}"
    )


# ---------------------------------------------------------------------------
# Token Exchange (authorization_code + PKCE verification)
# ---------------------------------------------------------------------------


@router.post("/oauth/token")
async def token(request: Request):
    """
    Token endpoint: exchange authorization code for access token.
    Requires code_verifier when the authorization used PKCE (S256).
    """
    form = await request.form()
    code = form.get("code")
    client_id = form.get("client_id")
    code_verifier = form.get("code_verifier", "").strip()
    grant_type = form.get("grant_type")

    if grant_type != "authorization_code":
        raise HTTPException(status_code=400, detail="Unsupported grant_type")

    code_data = _auth_codes.pop(code, None)
    if not code_data or code_data["client_id"] != client_id:
        raise HTTPException(status_code=400, detail="Invalid or expired code")

    if time.time() > code_data["expires_at"]:
        raise HTTPException(status_code=400, detail="Code expired")

    # PKCE: verify code_verifier against stored code_challenge
    stored_challenge = code_data.get("code_challenge")
    stored_method = code_data.get("code_challenge_method", "S256")
    if stored_challenge:
        if not code_verifier:
            raise HTTPException(
                status_code=400, detail="code_verifier required (PKCE)"
            )
        if not _pkce_verify(code_verifier, stored_challenge, stored_method):
            raise HTTPException(
                status_code=400, detail="Invalid code_verifier"
            )

    access_token = _mint_signed_access_token(client_id)

    return {
        "access_token": access_token,
        "token_type": "bearer",
        "expires_in": 3600,
    }
