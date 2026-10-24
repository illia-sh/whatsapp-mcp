"""Bearer-token authentication for the Streamable HTTP transport.

This is only used when the server is exposed over HTTP. Over stdio the
transport is a pipe from the process that launched us, so there is nothing to
authenticate and no token is required.

The model here is deliberately small: one pre-shared secret, held by the
operator, configured identically on the server and in the Claude connector's
request headers. It is not a user store. If more than one principal ever needs
access, move to OAuth rather than handing out copies of this secret.
"""

import os
import secrets
from typing import Optional, Tuple
from urllib.parse import urlsplit

from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.settings import AuthSettings
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import AnyHttpUrl

TOKEN_ENV = "WHATSAPP_MCP_TOKEN"
PUBLIC_URL_ENV = "WHATSAPP_MCP_PUBLIC_URL"

# Recorded on an accepted token. Nothing branches on scopes -- a caller either
# holds the shared secret or it does not -- but AccessToken requires the field.
_SCOPES = ["whatsapp"]

# 32 characters is roughly the shortest token worth calling a secret here;
# `secrets.token_urlsafe(32)` produces 43.
MIN_TOKEN_LENGTH = 32

_GENERATE_HINT = (
    "Generate one with: python3 -c 'import secrets; print(secrets.token_urlsafe(32))'"
)


class StaticTokenVerifier(TokenVerifier):
    """Accepts exactly one pre-shared bearer token."""

    def __init__(self, token: str) -> None:
        self._token = token

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        # Constant-time: a short-circuiting comparison leaks the token prefix to
        # anyone who can measure response times.
        if not secrets.compare_digest(token, self._token):
            return None
        return AccessToken(token=token, client_id="whatsapp-mcp", scopes=list(_SCOPES))


def build_auth() -> Tuple[TokenVerifier, AuthSettings]:
    """Build the verifier and settings for the HTTP transport.

    Raises RuntimeError rather than falling back to an unauthenticated server:
    this endpoint can read and send every message on the linked account, so
    refusing to start is the only safe response to a missing secret.
    """
    token = os.environ.get(TOKEN_ENV, "")
    if not token:
        raise RuntimeError(
            f"{TOKEN_ENV} must be set when serving over HTTP. {_GENERATE_HINT}"
        )
    if len(token) < MIN_TOKEN_LENGTH:
        raise RuntimeError(
            f"{TOKEN_ENV} must be at least {MIN_TOKEN_LENGTH} characters "
            f"(got {len(token)}). {_GENERATE_HINT}"
        )

    public_url = os.environ.get(PUBLIC_URL_ENV, "")
    if not public_url:
        raise RuntimeError(
            f"{PUBLIC_URL_ENV} must be set to the public HTTPS address Claude "
            f"will reach this server on, for example https://mcp.example.com"
        )
    if not public_url.startswith("https://"):
        # The token travels in a header on every request; without TLS it is
        # readable by anything on the path.
        raise RuntimeError(f"{PUBLIC_URL_ENV} must be an https:// URL, got {public_url!r}")

    url = AnyHttpUrl(public_url)
    settings = AuthSettings(
        issuer_url=url,
        resource_server_url=url,
        # No RFC 8707 resource indicator on a static token, so there is nothing
        # for the middleware to match against.
        validate_token_resource=False,
    )
    return StaticTokenVerifier(token), settings


# What the SDK allows by default when bound to loopback. Kept so that local
# health checks against 127.0.0.1 keep working alongside the public hostname.
_LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
_LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def build_transport_security(public_url: str) -> TransportSecuritySettings:
    """Allow the public hostname through DNS-rebinding protection.

    Binding to 127.0.0.1 makes the SDK enable DNS-rebinding protection with only
    loopback hosts allowed. Behind a reverse proxy that forwards the client's
    Host header, every real request then arrives as `Host: <public host>` and is
    refused with 421 -- even with a valid token. So the public host has to be
    added explicitly. Protection stays on: any other Host is still refused.
    """
    parts = urlsplit(public_url)
    hostname = parts.hostname or ""
    netloc = parts.netloc  # includes ":port" only when the URL names one

    allowed_hosts = list(_LOOPBACK_HOSTS)
    allowed_origins = list(_LOOPBACK_ORIGINS)
    for value in {netloc, hostname}:
        allowed_hosts.append(value)
    # A proxy may forward an explicit port, e.g. "mcp.example.com:443".
    allowed_hosts.append(f"{hostname}:*")
    # Origin is absent on server-to-server requests and the SDK accepts that;
    # this covers a client that does send one.
    allowed_origins.append(f"https://{netloc}")

    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
    )
