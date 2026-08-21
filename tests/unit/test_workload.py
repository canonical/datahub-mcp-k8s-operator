# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the pebble layer builder."""

import copy
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from charms.datahub_k8s.v0.datahub_client import (  # pylint: disable=E0611
    DatahubConnection,
)

import literals
import workload

CONNECTION = DatahubConnection(
    gms_url="http://gms:8080",
    token="pat",  # nosec B106
    service_account_urn="urn:li:corpuser:service_abc",
)

PROVIDER = SimpleNamespace(
    issuer_url="https://idp.example.com",
    introspection_endpoint="https://idp.example.com/introspect",
    jwks_endpoint="https://idp.example.com/.well-known/jwks.json",
    jwt_access_token=False,
    client_id="mcp-client",
    client_secret="mcp-secret",  # nosec
)


def _charm(*, provider=None, public_url=None, mutations=False, oauth_ready=True, registration=True):
    """Build a stub charm exposing everything workload.py reads."""
    return SimpleNamespace(
        config=SimpleNamespace(
            enable_mutation_tools=mutations,
            enable_client_registration=registration,
        ),
        datahub_relation=SimpleNamespace(connection=CONNECTION),
        oauth_relation=SimpleNamespace(provider_info=provider, is_ready=oauth_ready),
        public_url=public_url,
    )


class TestOauthEnvironment:
    """Tests for the client-authentication environment."""

    def test_is_empty_without_a_provider(self):
        """No relation means no verifier, so the endpoint stays open."""
        assert workload._oauth_environment(_charm()) == {}

    def test_is_empty_until_the_relation_is_usable(self):
        """A half-published provider must not produce a half-configured verifier."""
        charm = _charm(provider=PROVIDER, public_url="https://mcp.example.com", oauth_ready=False)

        assert workload._oauth_environment(charm) == {}

    def test_is_empty_without_a_public_url(self):
        """Clients could not discover the authorization server, so stay open."""
        assert workload._oauth_environment(_charm(provider=PROVIDER)) == {}

    def test_carries_the_introspection_credentials(self):
        """Verification needs the endpoint plus this server's own credentials."""
        env = workload._oauth_environment(_charm(provider=PROVIDER, public_url="https://mcp.example.com"))

        assert env["MCP_AUTH_INTROSPECTION_URL"] == "https://idp.example.com/introspect"
        assert env["MCP_AUTH_CLIENT_ID"] == "mcp-client"
        assert env["MCP_AUTH_CLIENT_SECRET"] == "mcp-secret"  # nosec B105

    def test_carries_the_registration_switch(self):
        """The workload cannot read charm config, so the decision is passed in."""
        env = workload._oauth_environment(_charm(provider=PROVIDER, public_url="https://mcp.example.com"))

        assert env["MCP_AUTH_CLIENT_REGISTRATION"] == "true"

    def test_reports_registration_turned_off(self):
        """Off is the setting that changes who the deployment serves, so it has to arrive."""
        charm = _charm(provider=PROVIDER, public_url="https://mcp.example.com", registration=False)

        assert workload._oauth_environment(charm)["MCP_AUTH_CLIENT_REGISTRATION"] == "false"

    def test_carries_the_issuer_for_discovery(self):
        """Clients are pointed at the authorization server after a 401."""
        env = workload._oauth_environment(_charm(provider=PROVIDER, public_url="https://mcp.example.com"))

        assert env["MCP_AUTH_ISSUER"] == "https://idp.example.com"

    def test_carries_how_the_provider_wants_tokens_checked(self):
        """The provider declares whether it signs its tokens, and where its keys are."""
        env = workload._oauth_environment(_charm(provider=PROVIDER, public_url="https://mcp.example.com"))

        assert env["MCP_AUTH_JWT_ACCESS_TOKEN"] == "false"
        assert env["MCP_AUTH_JWKS_URL"] == "https://idp.example.com/.well-known/jwks.json"

    def test_reports_a_provider_that_signs_its_tokens(self):
        """A signing provider lets the workload check tokens without calling it."""
        provider = copy.copy(PROVIDER)
        provider.jwt_access_token = True

        env = workload._oauth_environment(_charm(provider=provider, public_url="https://mcp.example.com"))

        assert env["MCP_AUTH_JWT_ACCESS_TOKEN"] == "true"

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("https://mcp.example.com", "https://mcp.example.com"),
            # Root-serving ingress providers publish a trailing slash, which
            # would otherwise produce a double-slash metadata URL.
            ("https://mcp.example.com/", "https://mcp.example.com"),
        ],
    )
    def test_advertises_the_public_url(self, url, expected):
        """401 responses point clients at the authorization server."""
        env = workload._oauth_environment(_charm(provider=PROVIDER, public_url=url))

        assert env["MCP_AUTH_BASE_URL"] == expected


class TestCompileEnvironment:
    """Tests for the full workload environment."""

    def test_binds_to_every_interface(self):
        """Binding to localhost would make the workload unreachable in the pod."""
        env = workload.compile_environment(_charm())

        assert env["FASTMCP_HOST"] == "0.0.0.0"  # nosec B104
        assert env["FASTMCP_PORT"] == str(literals.MCP_PORT)

    def test_disables_upstream_telemetry(self):
        """Charmed deployments do not phone home."""
        assert workload.compile_environment(_charm())["DATAHUB_TELEMETRY_ENABLED"] == "false"

    def test_propagates_the_model_proxy_settings(self):
        """The workload calls out to GMS and, with oauth, to the IdP."""
        with patch.dict(
            "os.environ",
            {"JUJU_CHARM_HTTPS_PROXY": "http://proxy:3128", "JUJU_CHARM_NO_PROXY": "10.0.0.0/8"},
        ):
            env = workload.compile_environment(_charm())

        assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://proxy:3128"
        assert env["NO_PROXY"] == env["no_proxy"] == "10.0.0.0/8"


class TestPebbleLayer:
    """Tests for the layer as a whole."""

    def test_health_check_targets_the_dedicated_route(self):
        """The check must not hit the MCP endpoint, which client auth protects."""
        check = workload.pebble_layer(_charm())["checks"]["up"]

        assert check["http"]["url"].endswith(literals.HEALTH_PATH)

    def test_restarts_the_workload_on_a_failing_check(self):
        """Pebble is the only thing that can rescue a hung start promptly."""
        service = workload.pebble_layer(_charm())["services"][literals.SERVICE_NAME]

        assert service["on-check-failure"] == {"up": "restart"}
