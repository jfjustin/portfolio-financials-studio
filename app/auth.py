"""Authentication — a private workspace behind one of three modes.

    AUTH_MODE = none      open (single local user; default for local/dev)
                password  one shared password gates the whole workspace
                entra     Microsoft Entra ID SSO (OIDC auth-code flow via MSAL)

Sessions are signed, HttpOnly cookies (itsdangerous). The middleware lets a small
allowlist through unauthenticated (health check, static assets, the login page and
auth callbacks); everything else requires a valid session.

Entra ID uses MSAL's confidential-client auth-code flow. It is only imported when
AUTH_MODE=entra, so the dependency is optional for local/password deployments.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse, Response

from .config import settings

COOKIE = "pfs_session"


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(payload: bytes) -> str:
    sig = hmac.new(settings.session_secret.encode(), payload, hashlib.sha256).digest()
    return f"{_b64e(payload)}.{_b64e(sig)}"


def _unsign(token: str) -> Optional[bytes]:
    try:
        body_b64, sig_b64 = token.split(".", 1)
        payload = _b64d(body_b64)
        expected = hmac.new(settings.session_secret.encode(), payload,
                            hashlib.sha256).digest()
        if hmac.compare_digest(expected, _b64d(sig_b64)):
            return payload
    except Exception:
        return None
    return None

# paths reachable without a session
_PUBLIC_PREFIXES = ("/static/", "/favicon")
_PUBLIC_EXACT = {"/login", "/api/login", "/logout", "/api/health",
                 settings.entra_redirect_path, "/auth/login"}


_DEFAULT_SECRET = "change-me-in-production"


def check_configuration() -> None:
    """Refuse to run a gated workspace on the secret shipped in the public
    repository (anyone could forge a session), or password mode with no
    password. Raised at startup, so the misconfiguration is seen at once."""
    if settings.auth_mode == "none":
        return
    if settings.session_secret == _DEFAULT_SECRET or len(settings.session_secret) < 32:
        raise RuntimeError("AUTH_MODE is on but SESSION_SECRET is unset or short: set it to "
                           "a random value of 32+ characters (openssl rand -hex 32)")
    if settings.auth_mode == "password" and not settings.app_password:
        raise RuntimeError("AUTH_MODE=password needs APP_PASSWORD")


def issue_session(resp: Response, user: str) -> None:
    payload = json.dumps({"u": user, "t": int(time.time())}).encode()
    resp.set_cookie(COOKIE, _sign(payload), httponly=True, samesite="lax",
                    secure=settings.session_cookie_secure,
                    max_age=settings.session_ttl_hours * 3600, path="/")


# --------------------------------------------------------------------------
# Password guessing: a few failures per address, then a pause
# --------------------------------------------------------------------------
_FAILURES: dict[str, list[float]] = {}


def client_address(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")     # App Service puts the caller first
    if fwd:
        return fwd.split(",")[0].strip().rsplit(":", 1)[0]
    return request.client.host if request.client else "?"


def _recent_failures(addr: str) -> list[float]:
    cutoff = time.time() - settings.login_lockout_minutes * 60
    kept = [t for t in _FAILURES.get(addr, []) if t > cutoff]
    if kept:
        _FAILURES[addr] = kept
    else:
        _FAILURES.pop(addr, None)
    return kept


def login_locked(request: Request) -> bool:
    return len(_recent_failures(client_address(request))) >= settings.login_max_failures


def record_login_failure(request: Request) -> None:
    addr = client_address(request)
    _FAILURES[addr] = _recent_failures(addr) + [time.time()]


def clear_login_failures(request: Request) -> None:
    _FAILURES.pop(client_address(request), None)


def clear_session(resp: Response) -> None:
    resp.delete_cookie(COOKIE, path="/")


# Set by Azure App Service Authentication (Easy Auth) on every request it lets
# through; the platform strips any copy a client sends, so it can be trusted.
_EASY_AUTH_USER = "x-ms-client-principal-name"


def current_user(request: Request) -> Optional[str]:
    if settings.auth_mode == "none":
        # behind Easy Auth the signed-in person is named, so the audit trail is too
        return request.headers.get(_EASY_AUTH_USER) or "local"
    tok = request.cookies.get(COOKIE)
    if not tok:
        return None
    payload = _unsign(tok)
    if payload is None:
        return None
    try:
        data = json.loads(payload)
    except Exception:
        return None
    if int(time.time()) - int(data.get("t", 0)) > settings.session_ttl_hours * 3600:
        return None
    return data.get("u")


def verify_password(candidate: str) -> bool:
    import hmac
    expected = settings.app_password or ""
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


class AuthMiddleware(BaseHTTPMiddleware):
    """Gate every request unless AUTH_MODE=none or the path is public."""

    async def dispatch(self, request: Request, call_next):
        if settings.auth_mode == "none":
            return await call_next(request)
        path = request.url.path
        if path in _PUBLIC_EXACT or path.startswith(_PUBLIC_PREFIXES):
            return await call_next(request)
        if current_user(request):
            return await call_next(request)
        # unauthenticated
        if path.startswith("/api/"):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return RedirectResponse("/login", status_code=302)


# --------------------------------------------------------------------------
# Entra ID (MSAL) — imported lazily; only used when AUTH_MODE=entra
# --------------------------------------------------------------------------
def _msal_app():
    import msal
    authority = f"https://login.microsoftonline.com/{settings.entra_tenant_id}"
    return msal.ConfidentialClientApplication(
        settings.entra_client_id, authority=authority,
        client_credential=settings.entra_client_secret)


def entra_auth_url(redirect_uri: str, state: str) -> str:
    return _msal_app().get_authorization_request_url(
        scopes=["User.Read"], redirect_uri=redirect_uri, state=state)


def entra_exchange_code(code: str, redirect_uri: str) -> Optional[str]:
    """Exchange an auth code for a token; return the user's email/UPN or None."""
    result = _msal_app().acquire_token_by_authorization_code(
        code, scopes=["User.Read"], redirect_uri=redirect_uri)
    if "id_token_claims" in result:
        claims = result["id_token_claims"]
        return (claims.get("preferred_username") or claims.get("email")
                or claims.get("name"))
    return None
