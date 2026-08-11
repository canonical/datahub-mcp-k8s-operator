# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""DataHub MCP charm integration test config.

These tests deploy the charm on its own, with no DataHub and no provider of any
kind. What they cover is what unit tests cannot: that the charm packs and
deploys, that the rock is pullable and its entrypoint imports against its own
dependency tree, and that refresh and scaling work.

Everything the charm does after DataHub publishes a token is covered by the
unit tests, which drive the same relation and secret through `ops.testing`, and
by `tests/serve`, which boots the real workload against a stub GMS.
"""

import logging
import os
from pathlib import Path

import jubilant
import pytest

logger = logging.getLogger(__name__)

APP_NAME = "datahub-mcp-k8s"
CONTAINER_NAME = "mcp-server"
RESOURCE_NAME = "datahub-mcp"
DEFAULT_IMAGE = "localhost:32000/datahub-mcp:latest"

NO_DATAHUB_MESSAGE = "missing required relation(s): datahub-client"

WAIT_TIMEOUT = 10 * 60


def pytest_addoption(parser):
    """Register the options the suite reads.

    Args:
        parser: The pytest command line parser.
    """
    parser.addoption("--charm-file", action="store", default=None, help="Path to the packed charm.")
    parser.addoption(
        "--datahub-mcp-image", action="store", default=DEFAULT_IMAGE, help="OCI image for the workload."
    )
    parser.addoption("--model", action="store", default=None, help="Model to test in.")
    parser.addoption("--keep-models", action="store_true", default=False)
    parser.addoption("--series", action="store", default=None)


def unit_message(juju: jubilant.Juju, app: str, unit: int = 0) -> str:
    """Return the workload status message of one of an application's units.

    Args:
        juju: The Juju client.
        app: Application name.
        unit: Unit index.

    Returns:
        The unit's workload status message.
    """
    return juju.status().apps[app].units[f"{app}/{unit}"].workload_status.message


def blocked_on_datahub(status, app: str = APP_NAME) -> bool:
    """Return whether every unit is blocked waiting for DataHub.

    Args:
        status: A Juju status object.
        app: Application name.

    Returns:
        True when all units report the expected blocked message.
    """
    units = status.apps[app].units.values()
    return bool(units) and all(
        unit.workload_status.current == "blocked" and unit.workload_status.message == NO_DATAHUB_MESSAGE
        for unit in units
    )


@pytest.fixture(scope="session")
def juju(request):
    """Return a Juju client, creating a temporary model when none is named.

    Args:
        request: The pytest fixture request.

    Yields:
        A jubilant Juju client.
    """
    model = request.config.getoption("--model") or os.environ.get("JUJU_MODEL")
    if model:
        client = jubilant.Juju(model=model)
        client.wait_timeout = WAIT_TIMEOUT
        yield client
        return

    with jubilant.temp_model() as client:
        client.wait_timeout = WAIT_TIMEOUT
        yield client


@pytest.fixture(scope="session")
def charm_file(request) -> str:
    """Return the path to the charm under test.

    Args:
        request: The pytest fixture request.

    Returns:
        The absolute path to the packed charm.
    """
    path = request.config.getoption("--charm-file") or os.environ.get("CHARM_PATH")
    if not path:
        matches = sorted(Path(__file__).resolve().parents[2].glob("*.charm"))
        assert matches, "no charm file found; run `make build-charm` or pass --charm-file"  # nosec B101
        path = str(matches[0])
    return str(Path(path).resolve())


@pytest.fixture(scope="session")
def mcp_image(request) -> str:
    """Return the OCI image the workload runs.

    Args:
        request: The pytest fixture request.

    Returns:
        The image reference.
    """
    return request.config.getoption("--datahub-mcp-image")


@pytest.fixture(scope="session")
def mcp_app(juju: jubilant.Juju, charm_file: str, mcp_image: str) -> str:
    """Deploy the charm on its own and wait for it to settle.

    Args:
        juju: The Juju client.
        charm_file: Path to the packed charm.
        mcp_image: OCI image for the workload.

    Returns:
        The deployed application name.
    """
    if APP_NAME not in juju.status().apps:
        juju.deploy(charm_file, app=APP_NAME, resources={RESOURCE_NAME: mcp_image})

    juju.wait(blocked_on_datahub, timeout=WAIT_TIMEOUT)
    return APP_NAME
