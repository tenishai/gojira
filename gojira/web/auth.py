"""IAP-trusting identity middleware — the Kodama pattern, ported.

IAP authenticates the human at the load balancer and asserts
x-goog-authenticated-user-email on every request it forwards. This
middleware trusts that header (see AuthConfig's warning about ever running
without IAP in front) and enforces the operator's allowlist. Two route
families keep their own auth and bypass this entirely:

  /webhook/*   — GitHub deliveries, HMAC-verified by the handler
  /api/v1/*    — agents: /usage carries cluster-scoped bearer tokens, the
                 onboarding routes (connect/status) carry auth.api_token
                 (see api_token_error below — required whenever this
                 middleware is enabled, so the exemption never means "no
                 auth" for a route that mutates state)

Everything else is the human UI. The resolved identity lands on
request.state.user_email, so handlers (and the provision ledger) can record
WHO acted — the audit trail a multi-user platform owes its operators.
"""

import logging
import secrets

from fastapi import Request
from fastapi.responses import JSONResponse

from gojira.config import AuthConfig

log = logging.getLogger(__name__)

IAP_HEADER = "x-goog-authenticated-user-email"
# /healthz: kubelet probes carry no identity — finding #17: the auth gate
# 401'd Gojira's own readiness probe and the pod could never become Ready.
EXEMPT_PREFIXES = ("/webhook/", "/api/v1/", "/healthz")


def identity_from(request: Request) -> str:
    """The IAP-asserted email, stripped of the accounts.google.com: prefix."""
    raw = request.headers.get(IAP_HEADER, "")
    return raw.removeprefix("accounts.google.com:").strip().lower()


def allowed(email: str, auth: AuthConfig) -> bool:
    if not email:
        return False
    if auth.allowed_domain and email.endswith("@" + auth.allowed_domain.lstrip("@")):
        return True
    return email in {e.strip().lower() for e in auth.allowed_emails}


def api_token_error(request: Request, auth: AuthConfig) -> JSONResponse | None:
    """Bearer-token gate for the privileged /api/v1 routes (api_connect,
    api_app_status) that live in EXEMPT_PREFIXES above.

    That exemption rests on the premise those routes carry their own auth —
    true for /api/v1/usage's cluster bearer tokens, but the onboarding routes
    had none, so with IAP enabled they were reachable anonymously. Call this
    first thing in each such route and return its result if not None.

    - A token configured: require an exact `Authorization: Bearer <token>`.
    - No token configured, IAP auth enabled: fail closed — a route with no
      auth of its own has no business exempting itself from IAP's.
    - No token configured, IAP auth disabled: allow (laptop / dev mode).
    """
    if auth.api_token:
        provided = request.headers.get("Authorization")
        expected = f"Bearer {auth.api_token}"
        # Non-None check first: compare_digest requires two strs (or two
        # bytes-likes) of matching type and raises on None, so a missing
        # header must short-circuit before it ever reaches the comparison.
        if provided is None or not secrets.compare_digest(provided, expected):
            return JSONResponse({"error": "bearer token required"}, status_code=401)
        return None
    if auth.enabled:
        return JSONResponse(
            {"error": "auth.api_token must be configured to use this endpoint"},
            status_code=401,
        )
    return None


def install(app, auth: AuthConfig) -> None:
    """Attach the middleware when auth is enabled; no-op otherwise."""
    if not auth.enabled:
        return

    @app.middleware("http")
    async def iap_gate(request: Request, call_next):
        if request.url.path.startswith(EXEMPT_PREFIXES):
            return await call_next(request)
        email = identity_from(request)
        if not email:
            # IAP never forwards unauthenticated traffic, so a missing header
            # means a misconfigured (or bypassed) front door — refuse loudly.
            log.warning("request without IAP identity: %s", request.url.path)
            return JSONResponse(
                {"detail": "no authenticated identity (is IAP in front?)"},
                status_code=401,
            )
        if not allowed(email, auth):
            log.warning("denied %s for %s", email, request.url.path)
            return JSONResponse({"detail": f"{email} is not authorized"}, status_code=403)
        request.state.user_email = email
        return await call_next(request)
