# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Upgrade tests for the DataHub MCP charm."""

import logging
from pathlib import Path

import jubilant

from tests.integration.conftest import RESOURCE_NAME, blocked_on_datahub

logger = logging.getLogger(__name__)


def test_refresh_replays_the_hooks_and_settles(
    juju: jubilant.Juju,
    mcp_app: str,
    charm_file: str,
    mcp_image: str,
):
    """A refresh replays upgrade-charm, config-changed and pebble-ready."""
    juju.refresh(mcp_app, path=Path(charm_file), resources={RESOURCE_NAME: mcp_image})
    juju.wait(blocked_on_datahub)

    status = juju.status().apps[mcp_app]
    assert not any(unit.workload_status.current == "error" for unit in status.units.values())
