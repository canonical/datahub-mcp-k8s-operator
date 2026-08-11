# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Shared fixtures for unit tests."""

from pathlib import Path

import pytest
from ops import testing

import literals
from charm import DatahubMcpK8SOperatorCharm

GMS_URL = "http://datahub-k8s.datahub.svc.cluster.local:8080"
SA_URN = "urn:li:corpuser:service_abc"
TOKEN_SECRET_ID = "secret:token"  # nosec B105


@pytest.fixture
def charm_ctx() -> testing.Context[DatahubMcpK8SOperatorCharm]:
    """Build a charm test context for state-transition tests."""
    charm_root = Path(__file__).resolve().parents[2]
    return testing.Context(DatahubMcpK8SOperatorCharm, charm_root=charm_root)


@pytest.fixture
def token_secret() -> testing.Secret:
    """Provide the DataHub token secret as granted over the relation."""
    return testing.Secret(id=TOKEN_SECRET_ID, tracked_content={"token": "pat"})  # nosec


@pytest.fixture
def datahub_relation(token_secret) -> testing.Relation:
    """Provide a fully published datahub-client relation."""
    return testing.Relation(
        endpoint=literals.DATAHUB_RELATION_NAME,
        remote_app_name="datahub-k8s",
        remote_app_data={
            "gms-url": GMS_URL,
            "secret-id": token_secret.id,
            "service-account-urn": SA_URN,
        },
    )


@pytest.fixture
def container() -> testing.Container:
    """Provide a reachable workload container."""
    return testing.Container(name=literals.CONTAINER_NAME, can_connect=True)


@pytest.fixture
def base_state(datahub_relation, token_secret, container) -> testing.State:
    """Provide the minimum state in which the charm reaches Active."""
    return testing.State(
        leader=True,
        relations={datahub_relation},
        secrets={token_secret},
        containers={container},
    )
