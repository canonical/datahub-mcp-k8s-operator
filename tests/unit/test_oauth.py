# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the oauth relation, from the charm's point of view."""

import dataclasses

import ops
from ops import testing

import literals

INGRESS_URL = "https://mcp.example.com/"
ISSUER = "https://idp.example.com"

_CLIENT_SECRET_ID = "secret:oauth-client"  # nosec B105

_PROVIDER_DATA = {
    "issuer_url": ISSUER,
    "authorization_endpoint": f"{ISSUER}/oauth2/auth",
    "token_endpoint": f"{ISSUER}/oauth2/token",
    "introspection_endpoint": f"{ISSUER}/admin/oauth2/introspect",
    "userinfo_endpoint": f"{ISSUER}/userinfo",
    "jwks_endpoint": f"{ISSUER}/.well-known/jwks.json",
    "scope": literals.OAUTH_SCOPE,
    "client_id": "mcp-client",
    "client_secret_id": _CLIENT_SECRET_ID,
}


def _oauth_state(base_state, *, with_ingress=True):
    """Return the base state plus a fully registered oauth relation."""
    client_secret = testing.Secret(id=_CLIENT_SECRET_ID, tracked_content={"secret": "mcp-secret"})  # nosec
    oauth = testing.Relation(
        endpoint=literals.OAUTH_RELATION_NAME,
        remote_app_name="idp",
        remote_app_data=_PROVIDER_DATA,
    )
    relations = base_state.relations | {oauth}
    if with_ingress:
        relations = relations | {
            testing.Relation(
                endpoint=literals.INGRESS_RELATION_NAME,
                remote_app_name="traefik",
                remote_app_data={"ingress": f'{{"url": "{INGRESS_URL}"}}'},
            )
        }
    return dataclasses.replace(
        base_state,
        relations=relations,
        secrets=base_state.secrets | {client_secret},
    )


def _service_env(state):
    """Return the workload environment the charm planned."""
    container = state.get_container(literals.CONTAINER_NAME)
    return container.layers[literals.SERVICE_NAME].services[literals.SERVICE_NAME].environment


def test_configures_token_introspection(charm_ctx, base_state):
    """A registered client turns the endpoint into an OAuth 2.1 resource server."""
    state = _oauth_state(base_state)

    out = charm_ctx.run(charm_ctx.on.config_changed(), state)

    assert out.unit_status == ops.ActiveStatus()
    env = _service_env(out)
    assert env["MCP_AUTH_INTROSPECTION_URL"] == _PROVIDER_DATA["introspection_endpoint"]
    assert env["MCP_AUTH_CLIENT_ID"] == "mcp-client"
    assert env["MCP_AUTH_CLIENT_SECRET"] == "mcp-secret"  # nosec B105
    # The trailing slash a root-serving ingress publishes must not leak into
    # the metadata URL advertised to clients.
    assert env["MCP_AUTH_BASE_URL"] == "https://mcp.example.com"


def test_publishes_the_client_config_once_the_url_is_known(charm_ctx, base_state):
    """The provider can only register a client after the public URL exists."""
    state = _oauth_state(base_state)

    out = charm_ctx.run(charm_ctx.on.config_changed(), state)

    oauth = next(r for r in out.relations if r.endpoint == literals.OAUTH_RELATION_NAME)
    assert literals.OAUTH_CALLBACK_PATH in oauth.local_app_data["redirect_uri"]
    assert "//oauth" not in oauth.local_app_data["redirect_uri"]


def test_client_authentication_survives_relation_removal(charm_ctx, base_state):
    """Removing the oauth relation reopens the endpoint rather than blocking."""
    out = charm_ctx.run(charm_ctx.on.config_changed(), base_state)

    assert out.unit_status == ops.ActiveStatus()
    assert "MCP_AUTH_INTROSPECTION_URL" not in _service_env(out)
