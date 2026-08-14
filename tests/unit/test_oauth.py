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


def _oauth_state(base_state, *, with_ingress=True, provider_data=None):
    """Return the base state plus an oauth relation, registered by default."""
    client_secret = testing.Secret(id=_CLIENT_SECRET_ID, tracked_content={"secret": "mcp-secret"})  # nosec
    oauth = testing.Relation(
        endpoint=literals.OAUTH_RELATION_NAME,
        remote_app_name="idp",
        remote_app_data=_PROVIDER_DATA if provider_data is None else provider_data,
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


def _layer(state):
    """Return the pebble layer the charm planned for the workload container."""
    return state.get_container(literals.CONTAINER_NAME).layers[literals.SERVICE_NAME]


def _service_env(state):
    """Return the workload environment the charm planned."""
    return _layer(state).services[literals.SERVICE_NAME].environment


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


def test_client_authentication_is_dropped_with_the_relation(charm_ctx, base_state):
    """Removing the oauth relation reopens the endpoint rather than blocking."""
    authenticated = charm_ctx.run(charm_ctx.on.config_changed(), _oauth_state(base_state))
    assert "MCP_AUTH_INTROSPECTION_URL" in _service_env(authenticated)

    oauth = next(r for r in authenticated.relations if r.endpoint == literals.OAUTH_RELATION_NAME)
    broken = charm_ctx.run(charm_ctx.on.relation_broken(oauth), authenticated)
    # The plan written while the relation existed is still on the container, so
    # the charm has to replan to clear it rather than simply stop writing it.
    departed = dataclasses.replace(broken, relations=broken.relations - {oauth})

    out = charm_ctx.run(charm_ctx.on.config_changed(), departed)

    assert out.unit_status == ops.ActiveStatus()
    assert "MCP_AUTH_INTROSPECTION_URL" not in _service_env(out)


class TestUnregisteredProvider:
    """Tests for an oauth relation the provider has not answered yet."""

    def test_publishes_the_client_config_when_the_relation_appears(self, charm_ctx, base_state):
        """Relating an IdP to a running deployment must not deadlock.

        The provider only publishes credentials once it has seen this charm's
        client config, and the library's own events only fire once those
        credentials exist. Nothing else would break the tie: with the ingress
        already up, no ingress event follows the relation.
        """
        state = _oauth_state(base_state, provider_data={})
        oauth = next(r for r in state.relations if r.endpoint == literals.OAUTH_RELATION_NAME)

        out = charm_ctx.run(charm_ctx.on.relation_created(oauth), state)

        published = next(r for r in out.relations if r.endpoint == literals.OAUTH_RELATION_NAME)
        assert literals.OAUTH_CALLBACK_PATH in published.local_app_data["redirect_uri"]

    def test_keeps_publishing_from_update_status(self, charm_ctx, base_state):
        """A missed relation event must self-heal rather than block forever."""
        state = _oauth_state(base_state, provider_data={})

        out = charm_ctx.run(charm_ctx.on.update_status(), state)

        published = next(r for r in out.relations if r.endpoint == literals.OAUTH_RELATION_NAME)
        assert "redirect_uri" in published.local_app_data
        assert isinstance(out.unit_status, ops.BlockedStatus)

    def test_blocks_until_the_client_is_registered(self, charm_ctx, base_state):
        """Serving here would publish the catalog to anyone who reaches the ingress."""
        state = _oauth_state(base_state, provider_data={})

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert isinstance(out.unit_status, ops.BlockedStatus)
        assert "register" in out.unit_status.message

    def test_blocks_when_the_provider_offers_no_way_to_check_a_token(self, charm_ctx, base_state):
        """Credentials alone are not enough: something has to verify the token."""
        without_endpoints = {**_PROVIDER_DATA, "introspection_endpoint": "", "jwks_endpoint": ""}
        state = _oauth_state(base_state, provider_data=without_endpoints)

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert isinstance(out.unit_status, ops.BlockedStatus)

    def test_stops_a_workload_that_is_already_serving(self, charm_ctx, base_state):
        """Relating an IdP must close an endpoint that was open, not leave it open."""
        running = charm_ctx.run(charm_ctx.on.config_changed(), base_state)
        serving = testing.Container(
            name=literals.CONTAINER_NAME,
            can_connect=True,
            layers={literals.SERVICE_NAME: _layer(running)},
            service_statuses={literals.SERVICE_NAME: ops.pebble.ServiceStatus.ACTIVE},
        )
        state = _oauth_state(dataclasses.replace(running, containers={serving}), provider_data={})

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert isinstance(out.unit_status, ops.BlockedStatus)
        statuses = out.get_container(literals.CONTAINER_NAME).service_statuses
        assert statuses[literals.SERVICE_NAME] == ops.pebble.ServiceStatus.INACTIVE
