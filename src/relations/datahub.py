# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Define the relation to DataHub."""

import logging
from typing import Optional

from charms.datahub_k8s.v0.datahub_client import (  # pylint: disable=E0611
    DatahubClientRequirer,
    DatahubConnection,
)
from ops import framework

import literals
from log import log_event_handler

logger = logging.getLogger(__name__)


class DatahubRelation(framework.Object):
    """Client for datahub-mcp:datahub-client relations.

    Attributes:
        charm: The charm this relation is attached to.
        requirer: The DatahubClientRequirer reading the relation databag.
        connection: Live DataHub connection details, or None.
    """

    def __init__(self, charm):
        """Construct.

        Args:
            charm: The charm to attach the hooks to.
        """
        super().__init__(charm, literals.DATAHUB_RELATION_NAME)
        self.charm = charm
        self.requirer = DatahubClientRequirer(charm, relation_name=literals.DATAHUB_RELATION_NAME)

        charm.framework.observe(
            charm.on[literals.DATAHUB_RELATION_NAME].relation_changed,
            self._on_relation_changed,
        )
        charm.framework.observe(
            charm.on[literals.DATAHUB_RELATION_NAME].relation_broken,
            self._on_relation_broken,
        )

    @property
    def connection(self) -> Optional[DatahubConnection]:
        """Return the current DataHub connection details, or None when not ready."""
        return self.requirer.get_connection()

    @log_event_handler(logger)
    def _on_relation_changed(self, event) -> None:
        """Handle datahub-client relation changed events.

        Args:
            event: The event triggered when the relation changed.
        """
        self.charm.reconcile()

    @log_event_handler(logger)
    def _on_relation_broken(self, event) -> None:
        """Handle datahub-client relation broken events.

        Args:
            event: The event triggered when the relation is removed.
        """
        self.charm.reconcile()
