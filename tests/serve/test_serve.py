# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the rock entrypoint's token checks."""

import asyncio

import httpx
import pytest
import respx
import serve
from fastmcp.server.auth.auth import AccessToken, TokenVerifier
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

    def test_callers_are_not_offered_the_document_route(self, oauth_env):
        """Fetching a caller-chosen URL is not something a filtered egress allows."""
        document = self._metadata(oauth_env, "/.well-known/oauth-authorization-server")

        assert "client_id_metadata_document_supported" not in document
        # Registration stays, so a caller still has a way to identify itself.
        assert document["registration_endpoint"] == f"{BASE_URL}/register"


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


class _StubVerifier(TokenVerifier):
    """Stand-in for the check a token passes before its client is looked at."""

    def __init__(self, access):
        """Construct.

        Args:
            access: What the wrapped check returns, None to refuse the token.
        """
        super().__init__(required_scopes=["openid"])
        self.access = access

    async def verify_token(self, token):
        """Return the prepared answer.

        Args:
            token: Ignored; the answer is fixed per instance.

        Returns:
            Whatever this stub was built with.
        """
        return self.access


def _token(**claims):
    """Return an access token carrying the given claims."""
    return AccessToken(token="a-token", client_id="unused", scopes=["openid"], claims=claims)  # nosec B106


class TestRegistrationSwitchParsing:
    """Tests for reading the switch out of the environment."""

    @pytest.mark.parametrize("value", ["false", "False", "FALSE"])
    def test_the_charm_can_turn_registration_off(self, monkeypatch, value):
        """The charm writes a lowercased boolean, but test the case anyway."""
        monkeypatch.setenv("MCP_AUTH_CLIENT_REGISTRATION", value)

        assert serve._client_registration_enabled() is False

    @pytest.mark.parametrize("value", ["true", "", "yes"])
    def test_anything_else_leaves_it_on(self, monkeypatch, value):
        """Only an explicit refusal narrows who may connect."""
        monkeypatch.setenv("MCP_AUTH_CLIENT_REGISTRATION", value)

        assert serve._client_registration_enabled() is True

    def test_an_unset_variable_leaves_it_on(self, monkeypatch):
        """A deployment that never set it keeps serving the callers it served before."""
        monkeypatch.delenv("MCP_AUTH_CLIENT_REGISTRATION", raising=False)

        assert serve._client_registration_enabled() is True


class TestRegistrationDisabledInFrontOfGoogle:
    """Tests for withdrawing registration from the server that offers it.

    This deployment is the registrar in front of Google, so turning registration
    off is something it can enforce itself rather than only refuse afterwards.
    """

    def _app(self, oauth_env, registration):
        """Return the routed application for a Google deployment.

        Args:
            oauth_env: Fixture setting the entrypoint's environment.
            registration: What the charm set the switch to.

        Returns:
            A Starlette app serving the provider's routes.
        """
        return Starlette(routes=self._provider(oauth_env, registration).get_routes("/mcp"))

    def _provider(self, oauth_env, registration):
        """Return the provider for a Google deployment.

        Args:
            oauth_env: Fixture setting the entrypoint's environment.
            registration: What the charm set the switch to.

        Returns:
            The auth provider the entrypoint builds.
        """
        oauth_env(
            client_id=CLIENT_ID,
            client_secret="s3cret",  # nosec B106
            issuer=GOOGLE_ISSUER,
            base_url=BASE_URL,
            introspection_url=GOOGLE_TOKENINFO,
            client_registration=registration,
        )
        return serve._auth_provider()

    def _register_a_caller(self, oauth_env):
        """Register a caller of its own, as one did before the switch was flipped.

        Args:
            oauth_env: Fixture setting the entrypoint's environment.

        Returns:
            The client identifier that caller came away with.
        """
        provider = self._provider(oauth_env, "true")
        client = TestClient(Starlette(routes=provider.get_routes("/mcp")))
        response = client.post("/register", json={"redirect_uris": ["http://localhost:1234/"]})
        return response.json()["client_id"]

    def test_registration_is_no_longer_advertised(self, oauth_env):
        """A caller reads this before trying, so it fails discovery rather than a request."""
        client = TestClient(self._app(oauth_env, "false"))

        document = client.get("/.well-known/oauth-authorization-server").json()

        assert "registration_endpoint" not in document

    def test_registration_is_no_longer_served(self, oauth_env):
        """Withdrawing the advertisement alone would leave it open to anyone who knew it."""
        client = TestClient(self._app(oauth_env, "false"))

        response = client.post("/register", json={"redirect_uris": ["http://localhost:1234/"]})

        assert response.status_code == 404

    def test_a_caller_registered_up_front_keeps_its_way_in(self, oauth_env):
        """The point is to leave exactly this caller, so it has to survive.

        The client held here is recognised without a registration, which is what
        an operator pastes into a caller they provisioned.
        """
        client = TestClient(self._app(oauth_env, "false"))

        document = client.get("/.well-known/oauth-authorization-server").json()

        assert document["authorization_endpoint"] == f"{BASE_URL}/authorize"
        assert document["token_endpoint"] == f"{BASE_URL}/token"

    def test_leaving_it_on_still_registers_callers(self, oauth_env):
        """The default has to stay what deployments already run."""
        client = TestClient(self._app(oauth_env, "true"))

        document = client.get("/.well-known/oauth-authorization-server").json()

        assert document["registration_endpoint"] == f"{BASE_URL}/register"

    def test_the_client_held_here_still_resolves(self, oauth_env):
        """Authorize and token both look the client up, so ours has to be found."""
        provider = self._provider(oauth_env, "false")

        client = asyncio.run(provider.get_client(CLIENT_ID))

        assert client is not None
        assert client.client_id == CLIENT_ID

    def test_a_caller_that_registered_earlier_is_cut_off(self, oauth_env):
        """Withdrawing the route leaves the registrations already handed out.

        Those outlive the switch, and neither authorize nor token consults it,
        so the caller has to be refused where the client is resolved instead.
        """
        registered = self._register_a_caller(oauth_env)
        provider = self._provider(oauth_env, "false")

        assert asyncio.run(provider.get_client(registered)) is None

    def test_that_caller_is_served_while_registration_is_on(self, oauth_env):
        """Otherwise the test above would pass on an empty store and prove nothing."""
        registered = self._register_a_caller(oauth_env)
        provider = self._provider(oauth_env, "true")

        assert asyncio.run(provider.get_client(registered)) is not None

    def test_only_the_switch_decides_which_proxy_is_built(self, oauth_env):
        """The gate is the whole difference, so it must not reach the default."""
        assert not isinstance(self._provider(oauth_env, "true"), serve.OwnClientOnlyProxy)
        assert isinstance(self._provider(oauth_env, "false"), serve.OwnClientOnlyProxy)


class TestRegistrationDisabledAtTheProvider:
    """Tests for refusing self-registered callers of a provider we do not run.

    Registration happens at the provider here, so there is no route to withdraw
    and the rule is applied to the tokens it hands out instead.
    """

    def _verifier(self, oauth_env, registration):
        """Return the verifier built for a non-Google deployment.

        Args:
            oauth_env: Fixture setting the entrypoint's environment.
            registration: What the charm set the switch to.

        Returns:
            The verifier `_token_verifier` selects.
        """
        oauth_env(
            client_id=CLIENT_ID,
            client_secret="s3cret",  # nosec B106
            issuer=ISSUER,
            base_url=BASE_URL,
            introspection_url=HYDRA_INTROSPECTION,
            client_registration=registration,
        )
        return serve._token_verifier(BASE_URL)

    def test_the_client_is_checked_when_registration_is_off(self, oauth_env):
        """Nothing else distinguishes a self-registered caller at the same provider."""
        assert isinstance(self._verifier(oauth_env, "false"), serve.OwnClientOnlyVerifier)

    def test_the_client_is_not_checked_when_registration_is_on(self, oauth_env):
        """A caller that registered itself is a legitimate caller by default."""
        assert isinstance(self._verifier(oauth_env, "true"), serve.CheckedIntrospectionVerifier)

    def test_offline_access_is_advertised(self, oauth_env):
        """A caller registered up front needs a refresh token to stay connected.

        Google rejects the scope, so it is only offered where it means something.
        """
        oauth_env(
            client_id=CLIENT_ID,
            client_secret="s3cret",  # nosec B106
            issuer=ISSUER,
            base_url=BASE_URL,
            introspection_url=HYDRA_INTROSPECTION,
        )
        app = Starlette(routes=serve._auth_provider().get_routes("/mcp"))

        document = TestClient(app).get("/.well-known/oauth-protected-resource/mcp").json()

        assert "offline_access" in document["scopes_supported"]


class TestOwnClientOnlyVerifier:
    """Tests for the check that a token belongs to the client this deployment owns."""

    def _verifier(self, access):
        """Return the verifier wrapping a check with a fixed answer.

        Args:
            access: What the wrapped check returns.

        Returns:
            An OwnClientOnlyVerifier.
        """
        return serve.OwnClientOnlyVerifier(_StubVerifier(access), CLIENT_ID)

    def test_accepts_a_token_issued_to_our_client(self):
        """The caller an operator provisioned holds exactly this client."""
        access = _token(client_id=CLIENT_ID)

        assert _verify(self._verifier(access)) is access

    def test_accepts_a_token_naming_our_client_as_the_authorized_party(self):
        """A signed token names the client in `azp` rather than `client_id`."""
        access = _token(azp=CLIENT_ID)

        assert _verify(self._verifier(access)) is access

    def test_rejects_a_token_issued_to_a_client_that_registered_itself(self):
        """This is the caller the deployment asked not to serve."""
        assert _verify(self._verifier(_token(client_id="self-registered"))) is None

    def test_rejects_a_token_naming_no_client(self):
        """A token that cannot be shown to be ours is not treated as ours."""
        assert _verify(self._verifier(_token(sub="a-user"))) is None

    def test_a_token_the_provider_rejected_stays_rejected(self):
        """The client check adds to the existing one rather than replacing it."""
        assert _verify(self._verifier(None)) is None
