# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the rock entrypoint's token checks."""

import asyncio

import httpx
import pytest
import respx
import serve
from fastmcp.server.auth.providers.jwt import JWTVerifier

BASE_URL = "https://mcp.example.com"
CLIENT_ID = "datahub-mcp"
GOOGLE_TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"
HYDRA_INTROSPECTION = "https://hydra.example.com/admin/oauth2/introspect"
JWKS_URL = "https://idp.example.com/.well-known/jwks.json"


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

    def test_google_uses_its_own_endpoint(self, oauth_env):
        """Google offers neither signed tokens nor the standard endpoint."""
        oauth_env(client_id=CLIENT_ID, client_secret="s3cret", introspection_url=GOOGLE_TOKENINFO)  # nosec B106

        assert isinstance(serve._token_verifier(BASE_URL), serve.GoogleAccessTokenVerifier)

    def test_any_other_provider_uses_the_standard_endpoint(self, oauth_env):
        """The standard check is the default, not a special case."""
        oauth_env(client_id=CLIENT_ID, client_secret="s3cret", introspection_url=HYDRA_INTROSPECTION)  # nosec B106

        assert isinstance(serve._token_verifier(BASE_URL), serve.CheckedIntrospectionVerifier)

    def test_no_way_to_check_leaves_authentication_off(self, oauth_env):
        """Without an endpoint or signing keys there is nothing to check against."""
        oauth_env(client_id=CLIENT_ID)

        assert serve._token_verifier(BASE_URL) is None


class TestGoogleAccessTokenVerifier:
    """Tests for the Google tokeninfo check."""

    def _verifier(self):
        """Return a verifier pointed at Google's endpoint."""
        return serve.GoogleAccessTokenVerifier(
            tokeninfo_url=GOOGLE_TOKENINFO,
            audiences=[CLIENT_ID, BASE_URL],
            base_url=BASE_URL,
        )

    @respx.mock
    def test_accepts_a_token_issued_for_us(self):
        """The normal path: Google confirms the token and it names us."""
        respx.get(GOOGLE_TOKENINFO).mock(
            return_value=httpx.Response(200, json={"aud": CLIENT_ID, "sub": "user-1", "scope": "openid email"})
        )

        access = _verify(self._verifier())

        assert access is not None
        assert access.subject == "user-1"
        assert access.scopes == ["openid", "email"]

    @respx.mock
    def test_rejects_a_token_issued_for_another_application(self):
        """Google confirms tokens for every application, so this is the real check."""
        respx.get(GOOGLE_TOKENINFO).mock(return_value=httpx.Response(200, json={"aud": "someone-else", "sub": "u"}))

        assert _verify(self._verifier()) is None

    @respx.mock
    def test_rejects_a_token_google_does_not_recognise(self):
        """Expired, revoked and unknown tokens all come back as a 400."""
        respx.get(GOOGLE_TOKENINFO).mock(return_value=httpx.Response(400, json={"error": "invalid_token"}))

        assert _verify(self._verifier()) is None

    @respx.mock
    def test_rejects_when_google_is_unreachable(self):
        """An unreachable provider must fail closed rather than let traffic through."""
        respx.get(GOOGLE_TOKENINFO).mock(side_effect=httpx.ConnectError("no route"))

        assert _verify(self._verifier()) is None


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
