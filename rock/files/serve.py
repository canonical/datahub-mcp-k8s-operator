# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Entrypoint for the charmed DataHub MCP server.

Runs the upstream `mcp-server-datahub` application unmodified, over the
streamable HTTP transport, optionally behind OAuth 2.1.

How a caller is authenticated depends on what the provider supports. A provider
that registers clients on demand is advertised to callers directly. Google does
not, so it is fronted by an **OAuth proxy**: an authorization server of our own
that registers callers itself and forwards them upstream. See `_auth_provider`.

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

from fastmcp.server.auth.auth import AccessToken, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.google import GoogleProvider
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
    """Build the token check for a provider that registers clients itself.

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

    return CheckedIntrospectionVerifier(
        audiences=audiences,
        introspection_url=_required("MCP_AUTH_INTROSPECTION_URL"),
        client_id=client_id,
        client_secret=_required("MCP_AUTH_CLIENT_SECRET"),
        base_url=base_url,
    )


def _auth_provider():
    """Build the auth provider, including its discovery metadata.

    An issuer with a public base URL is this deployment saying its callers
    authenticate. From that point on a missing piece is a startup failure, not
    a reason to open the endpoint: the process exits and pebble reports it.

    The provider depends on whether callers can register themselves with the
    identity provider. Where they can, it is named directly and this server only
    checks the tokens they arrive with. Google publishes no registration
    endpoint, so pointing callers at it leaves them with no way to obtain a
    client which is why Google is fronted by an OAuth proxy instead. The proxy
    is an authorization server in its own right: callers register with it and it
    holds the single Google client the deployment owns.

    Returns:
        An auth provider, or None when client authentication is disabled.
    """
    issuer = os.getenv("MCP_AUTH_ISSUER")
    base_url = os.getenv("MCP_AUTH_BASE_URL") or None
    if not issuer or not base_url:
        return None

    if _uses_google_tokeninfo(os.getenv("MCP_AUTH_INTROSPECTION_URL") or ""):
        # `GoogleProvider` is FastMCP's OAuth proxy preconfigured for Google.
        # Callers register with it and are redirected on to Google, whose own
        # redirect comes back here rather than to whichever loopback port the
        # caller happens to be listening on. That is what lets a deployment
        # register one fixed redirect URI with Google and serve every caller
        # with it.
        return GoogleProvider(
            client_id=_required("MCP_AUTH_CLIENT_ID"),
            client_secret=_required("MCP_AUTH_CLIENT_SECRET"),
            base_url=base_url,
            required_scopes=["openid"],
            valid_scopes=ADVERTISED_SCOPES,
        )

    # RemoteAuthProvider is what serves the metadata clients read after a 401,
    # so they can discover the identity provider on their own.
    return RemoteAuthProvider(
        token_verifier=_token_verifier(base_url),
        authorization_servers=[issuer],
        base_url=base_url,
        scopes_supported=ADVERTISED_SCOPES,
        resource_name="DataHub MCP Server",
    )


def main():
    """Start the MCP server on the streamable HTTP transport."""
    mcp = create_app()
    mcp.auth = _auth_provider()
    # `stateless_http` keeps no MCP session between requests, so any replica can
    # serve any MCP request.
    #
    # That alone does not make every deployment scalable. Against Google the
    # OAuth proxy is itself the authorization server, and it keeps its client
    # registrations and the tokens it issued on local disk. A replica recognises
    # only what it issued itself, so a request balanced to a different one is
    # rejected: run a single replica when fronting Google. Every other provider
    # is scalable, because there the token is checked against the provider
    # rather than against anything held here.
    mcp.run(transport="http", stateless_http=True, show_banner=False)


if __name__ == "__main__":
    main()
