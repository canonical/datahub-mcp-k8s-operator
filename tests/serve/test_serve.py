# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the rock entrypoint's token checks."""

import asyncio

import httpx
import pytest
import respx
import serve
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.auth.providers.jwt import JWTVerifier
from starlette.applications import Starlette
from starlette.testclient import TestClient

BASE_URL = "https://mcp.example.com"
CLIENT_ID = "datahub-mcp"
GOOGLE_ISSUER = "https://accounts.google.com"
GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
GOOGLE_SCOPE_URIS = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/userinfo.email",
]
HYDRA_INTROSPECTION = "https://hydra.example.com/admin/oauth2/introspect"
ISSUER = "https://idp.example.com"
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"


def _verify(verifier, token="a-token"):  # nosec B107
    """Run a verifier's async check from a synchronous test."""
    return asyncio.run(verifier.verify_token(token))


class TestAudienceMatches:
    """Tests for the audience check shared by every verifier."""

    def test_accepts_a_single_matching_value(self):
        """Most providers record one audience on the token."""
        assert serve._audience_matches(CLIENT_ID, [CLIENT_ID, BASE_URL]) is True

    def test_accepts_a_list_containing_a_match(self):
        """A token may name several audiences; one of ours is enough."""
        assert serve._audience_matches(["other-app", BASE_URL], [CLIENT_ID, BASE_URL]) is True

    def test_rejects_another_applications_token(self):
        """Without this the endpoint would accept any token from the provider."""
        assert serve._audience_matches("some-other-app", [CLIENT_ID, BASE_URL]) is False

    def test_rejects_a_missing_audience(self):
        """An unattributed token cannot be shown to have been issued for us."""
        assert serve._audience_matches(None, [CLIENT_ID, BASE_URL]) is False


class TestVerifierSelection:
    """Tests for which check the environment selects."""

    def test_signed_tokens_are_checked_locally(self, oauth_env):
        """A provider that signs its tokens needs no call to validate one."""
        oauth_env(
            client_id=CLIENT_ID,
            issuer="https://idp.example.com",
            jwks_url=JWKS_URL,
            jwt_access_token="true",  # nosec B106
            introspection_url=HYDRA_INTROSPECTION,
        )

        assert isinstance(serve._token_verifier(BASE_URL), JWTVerifier)

    def test_any_other_provider_uses_the_standard_endpoint(self, oauth_env):
        """The standard check is the default, not a special case."""
        oauth_env(client_id=CLIENT_ID, client_secret="s3cret", introspection_url=HYDRA_INTROSPECTION)  # nosec B106

        assert isinstance(serve._token_verifier(BASE_URL), serve.CheckedIntrospectionVerifier)

    @pytest.mark.parametrize(
        "environment",
        [
            # No endpoint and no signing keys: nothing to check a token against.
            {"client_id": CLIENT_ID},
            # A provider that signs its tokens but published no key set.
            {"client_id": CLIENT_ID, "jwt_access_token": "true", "issuer": ISSUER},  # nosec B105
            # An endpoint to ask, but no credential to ask it with.
            {"client_id": CLIENT_ID, "introspection_url": HYDRA_INTROSPECTION},
        ],
    )
    def test_a_missing_piece_is_a_startup_failure(self, oauth_env, environment):
        """Serving unauthenticated is never the answer once auth is configured."""
        oauth_env(**environment)

        with pytest.raises(ValueError):
            serve._token_verifier(BASE_URL)

    def test_the_provider_fails_rather_than_open_the_endpoint(self, oauth_env):
        """The entrypoint must not fall back to no authentication at all."""
        oauth_env(client_id=CLIENT_ID, issuer=ISSUER, base_url=BASE_URL)

        with pytest.raises(ValueError):
            serve._auth_provider()

    def test_no_issuer_at_all_leaves_authentication_off(self, oauth_env):
        """Without an oauth relation the charm sets none of these variables."""
        oauth_env()

        assert serve._auth_provider() is None


class TestGoogleAuthorizationServer:
    """Tests for the authorization server run in front of Google.

    Google publishes no registration endpoint, so a caller pointed at it has no
    way to obtain a client of its own. These cover what this deployment
    advertises in Google's place.
    """

    def _provider(self, oauth_env):
        """Return the provider built for a Google deployment.

        Args:
            oauth_env: Fixture setting the entrypoint's environment.

        Returns:
            The provider `_auth_provider` selects for Google.
        """
        oauth_env(
            client_id=CLIENT_ID,
            client_secret="s3cret",  # nosec B106
            issuer=GOOGLE_ISSUER,
            base_url=BASE_URL,
            introspection_url=GOOGLE_TOKENINFO,
        )
        return serve._auth_provider()

    def _metadata(self, oauth_env, path):
        """Return a metadata document as this deployment serves it.

        Args:
            oauth_env: Fixture setting the entrypoint's environment.
            path: The well-known path to fetch.

        Returns:
            The document, decoded.
        """
        app = Starlette(routes=self._provider(oauth_env).get_routes("/mcp"))
        return TestClient(app).get(path).json()

    def test_google_is_fronted_rather_than_advertised(self, oauth_env):
        """Google cannot register a caller, so it is not what callers are sent to."""
        assert isinstance(self._provider(oauth_env), GoogleProvider)

    def test_callers_are_offered_somewhere_to_register(self, oauth_env):
        """The missing registration endpoint is the whole reason this server exists."""
        document = self._metadata(oauth_env, "/.well-known/oauth-authorization-server")

        assert document["registration_endpoint"] == f"{BASE_URL}/register"

    def test_callers_are_pointed_at_us_and_not_at_google(self, oauth_env):
        """A caller sent straight to Google is the failure this replaces."""
        document = self._metadata(oauth_env, "/.well-known/oauth-protected-resource/mcp")

        assert document["authorization_servers"] == [f"{BASE_URL}/"]

    def test_the_scopes_to_ask_for_are_advertised(self, oauth_env):
        """A caller with nothing to request sends no scope, which Google rejects."""
        document = self._metadata(oauth_env, "/.well-known/oauth-authorization-server")

        assert document["scopes_supported"] == GOOGLE_SCOPE_URIS

    def test_only_openid_is_demanded_of_a_token(self, oauth_env):
        """Requiring the rest would reject a caller that asked for less."""
        provider = self._provider(oauth_env)

        assert provider.required_scopes == ["openid"]


class TestCheckedIntrospectionVerifier:
    """Tests for the audience check added on top of the standard endpoint."""

    def _verifier(self):
        """Return a verifier pointed at a standard introspection endpoint."""
        return serve.CheckedIntrospectionVerifier(
            audiences=[CLIENT_ID, BASE_URL],
            introspection_url=HYDRA_INTROSPECTION,
            client_id=CLIENT_ID,
            client_secret="s3cret",  # nosec B106
            base_url=BASE_URL,
        )

    @respx.mock
    def test_accepts_an_active_token_issued_for_us(self):
        """The normal path: the provider accepts it and it names us."""
        respx.post(HYDRA_INTROSPECTION).mock(
            return_value=httpx.Response(200, json={"active": True, "aud": [BASE_URL], "sub": "user-1"})
        )

        assert _verify(self._verifier()) is not None

    @respx.mock
    def test_rejects_an_active_token_issued_for_another_application(self):
        """The library checks only that the token is active, so this is ours to check."""
        respx.post(HYDRA_INTROSPECTION).mock(
            return_value=httpx.Response(200, json={"active": True, "aud": ["someone-else"], "sub": "u"})
        )

        assert _verify(self._verifier(), token="other-app-token") is None  # nosec B106

    @respx.mock
    def test_rejects_an_inactive_token(self):
        """Expired and revoked tokens are reported as inactive."""
        respx.post(HYDRA_INTROSPECTION).mock(return_value=httpx.Response(200, json={"active": False}))

        assert _verify(self._verifier(), token="inactive-token") is None  # nosec B106


@pytest.mark.parametrize(
    "url,expected",
    [
        (GOOGLE_TOKENINFO, True),
        (HYDRA_INTROSPECTION, False),
        ("https://oauth2.googleapis.com.evil.test/tokeninfo", False),
    ],
)
def test_google_is_recognised_by_host_alone(url, expected):
    """A lookalike hostname must not select Google's dialect."""
    assert serve._uses_google_tokeninfo(url) is expected
