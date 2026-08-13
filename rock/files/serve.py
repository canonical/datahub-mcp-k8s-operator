# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Entrypoint for the charmed DataHub MCP server.

Runs the upstream `mcp-server-datahub` application unmodified, over the
streamable HTTP transport, optionally behind an OAuth 2.1 token verifier.

Client authentication is off unless `MCP_AUTH_ISSUER` is set. The charm sets it,
and the variables below, from the `oauth` relation:

    MCP_AUTH_ISSUER             Identity provider that issues valid tokens.
    MCP_AUTH_JWT_ACCESS_TOKEN   "true" when the provider issues signed tokens.
    MCP_AUTH_JWKS_URL           Where to fetch the provider's signing keys.
    MCP_AUTH_INTROSPECTION_URL  Endpoint used to check an unsigned token.
    MCP_AUTH_CLIENT_ID          This deployment's OAuth client.
    MCP_AUTH_CLIENT_SECRET      Credential for calling the validation endpoint.
    MCP_AUTH_BASE_URL           Public URL of this server, advertised to clients.
"""

import os
from typing import Any, List, Optional
from urllib.parse import urlparse

import httpx
from fastmcp.server.auth.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.introspection import IntrospectionTokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from mcp_server_datahub.__main__ import create_app

# Google checks tokens through its own endpoint instead of the standard one.
GOOGLE_TOKENINFO_HOST = "oauth2.googleapis.com"

# Advertised to clients so they know what to ask the provider for.
ADVERTISED_SCOPES = ["openid", "profile", "email"]


def _audience_matches(claim: Any, expected: List[str]) -> bool:
    """Return whether a token was issued for this deployment.

    Args:
        claim: The audience recorded on the token, one value or a list.
        expected: The identifiers that mean "issued for us".

    Returns:
        True when the audience matches, False when it does not or is missing.
    """
    if claim is None:
        return False
    values = claim if isinstance(claim, list) else [claim]
    return any(value in expected for value in values)


class GoogleAccessTokenVerifier(TokenVerifier):
    """Check a Google access token through Google's tokeninfo endpoint.

    Google's tokens carry no signature to check locally, and it does not offer
    the standard validation endpoint, so this is the only way to check one.
    """

    def __init__(self, tokeninfo_url: str, audiences: List[str], **kwargs):
        """Construct.

        Args:
            tokeninfo_url: Google's tokeninfo endpoint.
            audiences: Identifiers that mean a token was issued for us.
            kwargs: Passed through to TokenVerifier (base_url, required_scopes).
        """
        super().__init__(**kwargs)
        self._tokeninfo_url = tokeninfo_url
        self._audiences = audiences

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Return the token's details, or None when it is not usable.

        Args:
            token: The bearer token presented by the client.

        Returns:
            An AccessToken when the token is valid and was issued for us,
            otherwise None.
        """
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    self._tokeninfo_url,
                    params={"access_token": token},
                    timeout=10,
                )
        except httpx.HTTPError:
            # Treat an unreachable provider as a failed check, not an open door.
            return None

        # Google reports an expired, revoked or unknown token as a 400.
        if response.status_code != 200:
            return None

        info = response.json()
        if not _audience_matches(info.get("aud"), self._audiences):
            return None

        return AccessToken(
            token=token,
            client_id=str(info.get("aud")),
            scopes=(info.get("scope") or "").split(),
            expires_at=int(info["exp"]) if info.get("exp") else None,
            subject=info.get("sub"),
            claims=info,
        )


class CheckedIntrospectionVerifier(IntrospectionTokenVerifier):
    """Introspection verifier that also checks who the token was issued for.

    The base class only asks the provider whether the token is still active, so
    on its own it would accept a token minted for any other application at the
    same provider.
    """

    def __init__(self, audiences: List[str], **kwargs):
        """Construct.

        Args:
            audiences: Identifiers that mean a token was issued for us.
            kwargs: Passed through to IntrospectionTokenVerifier.
        """
        super().__init__(**kwargs)
        self._audiences = audiences

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        """Return the token's details, or None when it is not usable.

        Args:
            token: The bearer token presented by the client.

        Returns:
            An AccessToken when the provider accepts the token and it was
            issued for us, otherwise None.
        """
        access = await super().verify_token(token)
        if access is None:
            return None
        if not _audience_matches((access.claims or {}).get("aud"), self._audiences):
            return None
        return access


def _uses_google_tokeninfo(introspection_url: str) -> bool:
    """Return whether the validation endpoint is Google's.

    Args:
        introspection_url: The endpoint published over the oauth relation.

    Returns:
        True for Google's tokeninfo endpoint, False for a standard one.
    """
    return urlparse(introspection_url).hostname == GOOGLE_TOKENINFO_HOST


def _required(name: str) -> str:
    """Return an environment variable that authentication cannot work without.

    Args:
        name: Name of the variable.

    Returns:
        Its value.

    Raises:
        ValueError: If the variable is unset or empty.
    """
    value = os.getenv(name)
    if not value:
        raise ValueError(f"client authentication is configured but {name} is not set")
    return value


def _token_verifier(base_url: str) -> TokenVerifier:
    """Build the token check described by the environment.

    Every path out of here either returns a verifier or raises. There is
    deliberately no "could not build one" return value: the caller has already
    established that this deployment authenticates its callers, and answering
    it with nothing would serve the catalog to anyone who can reach the port.

    Args:
        base_url: Public base URL of this server.

    Returns:
        A TokenVerifier.

    Raises:
        ValueError: If the environment names an issuer but describes no way to
            check a token against it.
    """
    client_id = _required("MCP_AUTH_CLIENT_ID")
    # A token issued for us names either this server or the client it was
    # issued through, depending on what the provider supports.
    audiences = [client_id, base_url]

    # Signed tokens prove themselves, so they are checked here against the
    # provider's public keys rather than by asking the provider every time.
    if os.getenv("MCP_AUTH_JWT_ACCESS_TOKEN") == "true":
        return JWTVerifier(
            jwks_uri=_required("MCP_AUTH_JWKS_URL"),
            issuer=_required("MCP_AUTH_ISSUER"),
            audience=audiences,
            base_url=base_url,
        )

    introspection_url = _required("MCP_AUTH_INTROSPECTION_URL")

    if _uses_google_tokeninfo(introspection_url):
        return GoogleAccessTokenVerifier(
            tokeninfo_url=introspection_url,
            audiences=audiences,
            base_url=base_url,
        )

    return CheckedIntrospectionVerifier(
        audiences=audiences,
        introspection_url=introspection_url,
        client_id=client_id,
        client_secret=_required("MCP_AUTH_CLIENT_SECRET"),
        base_url=base_url,
    )


def _auth_provider():
    """Build the auth provider, including its discovery metadata.

    An issuer with a public base URL is this deployment saying its callers
    authenticate. From that point on a missing piece is a startup failure, not
    a reason to open the endpoint: the process exits and pebble reports it.

    Returns:
        A RemoteAuthProvider, or None when client authentication is disabled.
    """
    issuer = os.getenv("MCP_AUTH_ISSUER")
    base_url = os.getenv("MCP_AUTH_BASE_URL") or None
    if not issuer or not base_url:
        return None

    verifier = _token_verifier(base_url)

    # RemoteAuthProvider is what serves the metadata clients read after a 401,
    # so they can discover the identity provider on their own.
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[issuer],
        base_url=base_url,
        scopes_supported=ADVERTISED_SCOPES,
        resource_name="DataHub MCP Server",
    )


def main():
    """Start the MCP server on the streamable HTTP transport."""
    mcp = create_app()
    mcp.auth = _auth_provider()
    # Stateless so that any replica can serve any request, which is what makes
    # the workload horizontally scalable behind a single ingress.
    mcp.run(transport="http", stateless_http=True, show_banner=False)


if __name__ == "__main__":
    main()
