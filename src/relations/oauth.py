# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Define the OAuth relation used to authenticate MCP clients."""

import logging
from typing import Optional

import ops
from charms.hydra.v0.oauth import (
    ClientConfig,
    ClientConfigError,
    OauthProviderConfig,
    OAuthRequirer,
)
from ops import framework

import exceptions
import literals
from log import log_event_handler

logger = logging.getLogger(__name__)


class OauthRelation(framework.Object):
    """Client for datahub-mcp:oauth relations.

    The MCP server is an OAuth 2.1 resource server: it does not run a browser
    login flow, it checks the bearer tokens its callers already hold. The
    relation indicates how to do that:
    - where the provider's signing keys are when it signs its tokens
    - which endpoint to ask when it does not

    Attributes:
        charm: The charm this relation is attached to.
        requirer: The OAuthRequirer instance handling the relation databags.
        is_related: Whether an oauth relation currently exists.
        provider_info: Live provider details or None.
    """

    def __init__(self, charm):
        """Construct.

        Args:
            charm: The charm to attach the hooks to.
        """
        super().__init__(charm, literals.OAUTH_RELATION_NAME)
        self.charm = charm

        # The client config depends on the ingress URL, which may not be known
        # yet; it is published from `reconcile()` once available.
        self.requirer = OAuthRequirer(charm, client_config=None, relation_name=literals.OAUTH_RELATION_NAME)

        charm.framework.observe(self.requirer.on.oauth_info_changed, self._on_oauth_info_changed)
        charm.framework.observe(self.requirer.on.oauth_info_removed, self._on_oauth_info_removed)
        charm.framework.observe(self.requirer.on.invalid_client_config, self._on_invalid_client_config)

    @property
    def is_related(self) -> bool:
        """Return whether an oauth relation currently exists."""
        return self.charm.model.get_relation(literals.OAUTH_RELATION_NAME) is not None

    @property
    def provider_info(self) -> Optional[OauthProviderConfig]:
        """Return the current provider details, or None when not available."""
        if not self.is_related:
            return None
        if not self.requirer.is_client_created():
            return None
        try:
            return self.requirer.get_provider_info()
        except (ops.SecretNotFoundError, ops.ModelError) as e:
            logger.info("oauth client secret not accessible yet: %s", e)
            return None

    def publish_client_config(self) -> None:
        """Publish the OAuth client config so the provider can register the client.

        Registration is what yields the client credentials used to call the
        introspection endpoint. Called from `reconcile()` once the ingress URL is known.

        Raises:
            UnreadyStateError: If the client config is rejected by the library.
        """
        if not self.is_related:
            return

        url = self.charm.public_url
        if not url:
            return

        client_config = ClientConfig(
            redirect_uri=f"{url.rstrip('/')}{literals.OAUTH_CALLBACK_PATH}",
            scope=literals.OAUTH_SCOPE,
            grant_types=literals.OAUTH_GRANT_TYPES,
        )
        try:
            self.requirer.update_client_config(client_config)
        except ClientConfigError as e:
            raise exceptions.UnreadyStateError(f"invalid OAuth client config: {e}") from None

    @log_event_handler(logger)
    def _on_oauth_info_changed(self, event) -> None:
        """Handle oauth-info-changed events from the provider.

        Args:
            event: The event triggered when the provider publishes client credentials.
        """
        self.charm.reconcile()

    @log_event_handler(logger)
    def _on_oauth_info_removed(self, event) -> None:
        """Handle oauth-info-removed events when the relation is broken.

        Args:
            event: The event triggered when the relation is removed.
        """
        self.charm.reconcile()

    @log_event_handler(logger)
    def _on_invalid_client_config(self, event) -> None:
        """Handle invalid-client-config events from the oauth library.

        Args:
            event: The event carrying the validation error.
        """
        logger.error("invalid OAuth client config: %s", event.error)
        self.charm.unit.status = ops.BlockedStatus("invalid OAuth client config")
