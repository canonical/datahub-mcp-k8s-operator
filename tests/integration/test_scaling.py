# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Scaling tests for the DataHub MCP charm."""

import logging

import jubilant

from tests.integration.conftest import blocked_on_datahub

logger = logging.getLogger(__name__)


def test_scale_out_and_in(juju: jubilant.Juju, mcp_app: str):
    """Every unit reaches the same state, and losing units leaves the rest healthy."""
    juju.add_unit(mcp_app, num_units=2)
    juju.wait(lambda status: len(status.apps[mcp_app].units) == 3 and blocked_on_datahub(status))

    assert len(juju.status().apps[mcp_app].units) == 3

    juju.remove_unit(mcp_app, num_units=2)
    juju.wait(lambda status: len(status.apps[mcp_app].units) == 1 and blocked_on_datahub(status))

    assert len(juju.status().apps[mcp_app].units) == 1
