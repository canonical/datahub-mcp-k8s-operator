# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Build the pebble layer that runs the DataHub MCP server."""

import logging
import os
from typing import Dict

import literals

logger = logging.getLogger(__name__)


def compile_environment(charm) -> Dict[str, str]:
    """Build the workload environment for the current charm state.

    Args:
        charm: The charm to read config and relation state from.

    Returns:
        The environment for the pebble service.
    """
    connection = charm.datahub_relation.connection
    assert connection is not None  # nosec B101

    env = {
        "PYTHONPATH": literals.SERVER_LIB_PATH,
        "DATAHUB_GMS_URL": connection.gms_url,
        "DATAHUB_GMS_TOKEN": connection.token,
        "DATAHUB_TELEMETRY_ENABLED": "false",
        "FASTMCP_HOST": "0.0.0.0",  # nosec B104
        "FASTMCP_PORT": str(literals.MCP_PORT),
        "TOOLS_IS_MUTATION_ENABLED": str(charm.config.enable_mutation_tools).lower(),
    }
    env.update(_oauth_environment(charm))
    env.update(_proxy_environment())
    return env


def _oauth_environment(charm) -> Dict[str, str]:
    """Build the client-authentication part of the environment.

    Empty when there is no usable oauth relation, which leaves the endpoint
    unauthenticated and relying on whatever the ingress enforces.

    Args:
        charm: The charm to read relation state from.

    Returns:
        The OAuth environment variables, possibly empty.
    """
    provider = charm.oauth_relation.provider_info
    if provider is None:
        return {}
    if not provider.client_id or not provider.client_secret:
        logger.info("oauth provider has not issued client credentials yet")
        return {}

    if not charm.public_url:
        # Without a public URL there is no protected-resource metadata to
        # advertise, so clients could not discover where to get a token.
        logger.info("no public URL yet, leaving client authentication off")
        return {}

    return {
        # The issuer is what the workload advertises to clients as the
        # authorization server to go to after a 401.
        "MCP_AUTH_ISSUER": provider.issuer_url,
        # How the workload checks a token: against the provider's public keys
        # when the provider signs them or by asking the provider when it does not.
        "MCP_AUTH_JWT_ACCESS_TOKEN": str(bool(provider.jwt_access_token)).lower(),
        "MCP_AUTH_JWKS_URL": provider.jwks_endpoint,
        "MCP_AUTH_INTROSPECTION_URL": provider.introspection_endpoint,
        "MCP_AUTH_CLIENT_ID": provider.client_id,
        "MCP_AUTH_CLIENT_SECRET": provider.client_secret,
        "MCP_AUTH_BASE_URL": charm.public_url.rstrip("/"),
    }


def _proxy_environment() -> Dict[str, str]:
    """Propagate the model's proxy settings to the workload.

    Returns:
        The proxy environment variables, possibly empty.
    """
    env = {}
    for juju_var, names in (
        ("JUJU_CHARM_HTTP_PROXY", ("HTTP_PROXY", "http_proxy")),
        ("JUJU_CHARM_HTTPS_PROXY", ("HTTPS_PROXY", "https_proxy")),
        ("JUJU_CHARM_NO_PROXY", ("NO_PROXY", "no_proxy")),
    ):
        value = os.getenv(juju_var)
        if value:
            env.update({name: value for name in names})
    return env


def pebble_layer(charm) -> Dict:
    """Build the pebble layer for the MCP server.

    Args:
        charm: The charm to read config and relation state from.

    Returns:
        The pebble layer as a dict. It carries no top-level `summary`, so it
        compares equal to the merged plan pebble reports back which is what
        `update-status` uses to detect drift.
    """
    return {
        "services": {
            literals.SERVICE_NAME: {
                "summary": "DataHub MCP server",
                "command": f"{literals.PYTHON_BIN_PATH} {literals.SERVER_ENTRYPOINT}",
                "startup": "enabled",
                "override": "replace",
                "environment": compile_environment(charm),
                "on-check-failure": {"up": "restart"},
            }
        },
        "checks": {
            "up": {
                "override": "replace",
                "period": "10s",
                "threshold": literals.HEALTHCHECK_FAILURE_THRESHOLD,
                # A dedicated health route, so the check never touches the MCP
                # endpoint itself, and stays reachable when client auth is on.
                "http": {"url": f"http://localhost:{literals.MCP_PORT}{literals.HEALTH_PATH}"},
            }
        },
    }
