# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Integration tests for the DataHub MCP charm."""

import logging

import jubilant

from tests.integration.conftest import (
    CONTAINER_NAME,
    NO_DATAHUB_MESSAGE,
    blocked_on_datahub,
    unit_message,
)

logger = logging.getLogger(__name__)

# Importing the entrypoint the way the pebble service does proves the rock
# carries every dependency it needs. The charm never starts the service without
# DataHub, so nothing else in this suite would notice a broken rock.
IMPORT_ENTRYPOINT = "env PYTHONPATH=/opt/mcp:/opt/mcp/lib /usr/bin/python3.12 -c 'import serve; print(serve.__file__)'"


def test_blocks_without_the_datahub_relation(juju: jubilant.Juju, mcp_app: str):
    """The endpoint has nothing to serve until a catalog is related."""
    assert unit_message(juju, mcp_app) == NO_DATAHUB_MESSAGE


def test_the_rock_entrypoint_imports_in_the_workload_container(juju: jubilant.Juju, mcp_app: str):
    """The rock must carry fastmcp, the DataHub SDK and everything they pull in."""
    output = juju.cli("ssh", "--container", CONTAINER_NAME, f"{mcp_app}/0", IMPORT_ENTRYPOINT, include_model=True)

    assert "/opt/mcp/serve.py" in output, output


def test_accepts_a_configuration_change(juju: jubilant.Juju, mcp_app: str):
    """Config handling must not depend on the charm having reached Active."""
    juju.config(mcp_app, {"enable-mutation-tools": "true"})
    try:
        juju.wait(blocked_on_datahub)
    finally:
        juju.config(mcp_app, {"enable-mutation-tools": "false"})
        juju.wait(blocked_on_datahub)
