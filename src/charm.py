#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm the DataHub MCP server."""

import logging
from typing import Optional

import ops
from charms.data_platform_libs.v0.data_models import TypedCharmBase
from charms.traefik_k8s.v2.ingress import IngressPerAppRequirer
from ops.pebble import CheckStatus

import exceptions
import literals
import workload
from config import CharmConfig
from log import log_event_handler
from relations.datahub import DatahubRelation
from relations.oauth import OauthRelation

logger = logging.getLogger(__name__)


class DatahubMcpK8SOperatorCharm(TypedCharmBase[CharmConfig]):
    """Charm the DataHub MCP server.

    Attributes:
        config_type: Class used to store the config.
        public_url: URL the endpoint is served on, or None without ingress.
    """

    config_type = CharmConfig

    def __init__(self, framework: ops.Framework):
        """Construct.

        Args:
            framework: Object to initialize the charm instance.
        """
        super().__init__(framework)

        self.framework.observe(self.on[literals.CONTAINER_NAME].pebble_ready, self._on_pebble_ready)
        self.framework.observe(self.on.config_changed, self._on_config_changed)
        self.framework.observe(self.on.secret_changed, self._on_secret_changed)
        self.framework.observe(self.on.peer_relation_changed, self._on_peer_relation_changed)
        self.framework.observe(self.on.update_status, self._on_update_status)

        # DataHub: supplies the GMS URL and a read-only service account token.
        self.datahub_relation = DatahubRelation(self)

        # OAuth: lets the endpoint verify the bearer tokens its callers present.
        self.oauth_relation = OauthRelation(self)

        # Ingress. `strip_prefix=True` so the provider strips the per-app path
        # prefix before forwarding: the MCP transport lives at `/mcp` on the
        # workload regardless of where it is published.
        self.ingress = IngressPerAppRequirer(
            self,
            relation_name=literals.INGRESS_RELATION_NAME,
            port=literals.MCP_PORT,
            strip_prefix=True,
        )
        self.framework.observe(self.ingress.on.ready, self._on_ingress_changed)
        self.framework.observe(self.ingress.on.revoked, self._on_ingress_changed)

    @property
    def public_url(self) -> Optional[str]:
        """Return the URL the endpoint is served on, or None without ingress."""
        if not self.ingress.is_ready():
            return None
        return self.ingress.url

    @log_event_handler(logger)
    def _on_pebble_ready(self, event) -> None:
        """Handle pebble-ready events.

        Args:
            event: The event triggered when the workload container is ready.
        """
        self.reconcile()

    @log_event_handler(logger)
    def _on_config_changed(self, event) -> None:
        """Handle config-changed events.

        Args:
            event: The event triggered when the configuration is changed.
        """
        self.reconcile()

    @log_event_handler(logger)
    def _on_secret_changed(self, event) -> None:
        """Handle secret-changed events.

        Both credentials the workload holds arrive in provider-owned secrets
        so reconcile whenever the relation that could own the secret exists.

        Args:
            event: The secret-changed event fired by Juju.
        """
        if self.datahub_relation.requirer.is_related or self.oauth_relation.is_related:
            self.reconcile()

    @log_event_handler(logger)
    def _on_peer_relation_changed(self, event) -> None:
        """Handle peer-relation-changed events.

        Args:
            event: The event triggered when the peer relation is changed.
        """
        self.reconcile()

    @log_event_handler(logger)
    def _on_ingress_changed(self, event) -> None:
        """Handle ingress ready and revoked events.

        Args:
            event: The ingress ready or revoked event.
        """
        self.reconcile()

    @log_event_handler(logger)
    def _on_update_status(self, event) -> None:
        """Report workload health and repair a drifted or missing pebble plan.

        Args:
            event: The update-status event triggered at regular intervals.
        """
        self._refresh_ingress_address()

        container = self.unit.get_container(literals.CONTAINER_NAME)
        if not container.can_connect():
            self.unit.status = ops.MaintenanceStatus("status check: NOT READY")
            return

        try:
            self._check_state()
        except exceptions.UnreadyStateError as err:
            self._stop_workload()
            self.unit.status = ops.BlockedStatus(str(err))
            return

        try:
            check = container.get_check("up")
        except ops.ModelError:
            logger.info("invalid plan (missing check), replanning")
            self.reconcile()
            return

        if dict(container.get_plan().to_dict()) != workload.pebble_layer(self):
            logger.info("invalid plan (out of sync), replanning")
            self.reconcile()
            return

        if check.status != CheckStatus.UP:
            logger.info("up check failed (failures=%d)", check.failures)
            self.unit.status = ops.MaintenanceStatus("status check: DOWN")
            return

        self.reconcile()

    def _check_state(self) -> None:
        """Check whether the charm has everything it needs to run the workload.

        Raises:
            UnreadyStateError: If a required relation has not published usable
                data yet.
        """
        if not self.datahub_relation.requirer.is_related:
            raise exceptions.UnreadyStateError(f"missing required relation(s): {literals.DATAHUB_RELATION_NAME}")

        if self.datahub_relation.connection is None:
            raise exceptions.UnreadyStateError("waiting for DataHub to publish the access token")

        if not self.oauth_relation.is_related:
            return

        if not self.public_url:
            raise exceptions.UnreadyStateError(
                "OAuth is enabled but the 'ingress' relation is not ready; "
                "clients need a public URL to discover the authorization server"
            )

        # Relating an identity provider is how a deployment says its callers must
        # authenticate. Until the provider has answered with credentials the
        # workload has no way to check a token, and serving anyway would publish
        # the catalog to anyone who can reach the ingress.
        if not self.oauth_relation.is_ready:
            raise exceptions.UnreadyStateError("waiting for the OAuth provider to register the client")

    def _refresh_ingress_address(self) -> None:
        """Republish the unit's address on the ingress relation.

        The ingress library publishes the address only on relation churn, leader
        election and charm upgrade. On K8s those all land in the first seconds of
        a rescheduled unit's life, when Juju can still report the departed pod's
        address; whatever gets written then is never revisited, so the ingress
        keeps routing to a dead IP.
        """
        self.ingress.provide_ingress_requirements(port=literals.MCP_PORT)

    def reconcile(self) -> None:
        """Reconcile the charm to its desired state.

        Single entry point for every observer. Reads current config, relation
        state and secrets, decides whether the charm is ready, then ensures the
        workload's pebble plan matches.
        """
        try:
            # Publishing comes first: the provider cannot register the client,
            # and so cannot become ready, until it has seen the client config.
            self.oauth_relation.publish_client_config()
            self._check_state()
        except exceptions.UnreadyStateError as err:
            self._stop_workload()
            self.unit.status = ops.BlockedStatus(str(err))
            return

        self.unit.set_ports(literals.MCP_PORT)

        container = self.unit.get_container(literals.CONTAINER_NAME)
        if not container.can_connect():
            self.unit.status = ops.MaintenanceStatus("waiting for the workload container")
            return

        container.add_layer(literals.SERVICE_NAME, workload.pebble_layer(self), combine=True)
        try:
            container.replan()
        except ops.pebble.ChangeError as e:
            logger.warning("Pebble replan failed: %s", str(e))
            self.unit.status = ops.MaintenanceStatus("replan failed")
            return

        self.unit.status = ops.ActiveStatus()

    def _stop_workload(self) -> None:
        """Stop the workload, if it is running, because the charm is not ready."""
        container = self.unit.get_container(literals.CONTAINER_NAME)
        if not container.can_connect():
            return
        services = container.get_services(literals.SERVICE_NAME)
        if any(service.is_running() for service in services.values()):
            logger.info("stopping the workload while the charm is not ready to serve")
            container.stop(literals.SERVICE_NAME)


if __name__ == "__main__":  # pragma: nocover
    ops.main(DatahubMcpK8SOperatorCharm)  # type: ignore
