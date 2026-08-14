# Copyright 2024 Canonical Ltd.
# See LICENSE file for licensing details.

"""Library for the datahub_client relation.

This library provides the DatahubClientProvider and DatahubClientRequirer classes that
handle the provider and the requirer sides of the datahub_client interface.

The interface lets a charm consume the DataHub GMS API as a dedicated DataHub service
account. The provider (datahub-k8s) creates the service account, mints a Personal
Access Token for it, stores the token in a Juju secret granted to the relation, and
publishes the GMS URL alongside the secret ID. The requirer reads both and configures
its workload.

Provider application databag (every field is a string):

    gms-url               URL of the GMS API, reachable from the requirer.
                          "http://datahub-k8s.datahub-k8s.svc.cluster.local:8080"
    secret-id             ID of a Juju secret whose `token` key holds the PAT.
                          "secret://3a214b83-9add-45e4-83af-665104bec574/akrgmperhr88jppn48ug"
    service-account-urn   URN of the DataHub service account the token acts as.
                          "urn:li:corpuser:service_cb8b10ee-6bb0-4db6-9fc3-337eb588cc2c"

Token lifetime: the PAT does not expire and is not rotated on a schedule. It is
tied to the service account, so removing the relation deletes the account and
invalidates the token with it. The provider also re-mints a token whenever the
service account it belongs to is gone, which is what makes revoking an account
in DataHub a working way to force a new one.
"""

import logging
from dataclasses import dataclass
from typing import Optional

from ops.charm import CharmBase
from ops.framework import Object
from ops.model import ModelError, Relation, SecretNotFoundError

# The unique Charmhub library identifier, never change it
LIBID = "e20436709cfa48f289bf37c08b53b482"

# Increment this major API version when introducing breaking changes
LIBAPI = 0

# Increment this PATCH version before using `charmcraft publish-lib` or reset
# to 0 if you are raising the major API version
LIBPATCH = 2

logger = logging.getLogger(__name__)

DEFAULT_RELATION_NAME = "datahub-client"

GMS_URL_FIELD = "gms-url"
SECRET_ID_FIELD = "secret-id"  # nosec B105
SERVICE_ACCOUNT_URN_FIELD = "service-account-urn"
TOKEN_SECRET_KEY = "token"  # nosec B105


@dataclass(frozen=True)
class DatahubConnection:
    """Everything a requirer needs to talk to DataHub GMS.

    Attributes:
        gms_url: URL of the GMS API.
        token: DataHub Personal Access Token to authenticate with.
        service_account_urn: URN of the service account the token acts as.
    """

    gms_url: str
    token: str
    service_account_urn: str


class DatahubClientProvider(Object):
    """Provider side of the datahub_client relation.

    The library owns the databag layout; the charm owns the DataHub-side
    resources (service account, access token) and the Juju secret.
    """

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """Construct.

        Args:
            charm: The charm instance.
            relation_name: Name of the relation.
        """
        super().__init__(charm, relation_name)
        self.charm = charm
        self.relation_name = relation_name

    def get_service_account_urn(self, relation: Relation) -> Optional[str]:
        """Return the service account URN previously published on a relation.

        Args:
            relation: The relation to read.

        Returns:
            The URN, or None if nothing has been published yet.
        """
        return relation.data[self.charm.app].get(SERVICE_ACCOUNT_URN_FIELD) or None

    def get_secret_id(self, relation: Relation) -> Optional[str]:
        """Return the token secret ID previously published on a relation.

        Args:
            relation: The relation to read.

        Returns:
            The Juju secret ID, or None if nothing has been published yet.
        """
        return relation.data[self.charm.app].get(SECRET_ID_FIELD) or None

    def publish_connection(
        self,
        relation: Relation,
        gms_url: str,
        secret_id: str,
        service_account_urn: str,
    ) -> None:
        """Publish the connection details on a relation.

        Args:
            relation: The relation to update.
            gms_url: URL of the GMS API reachable from the requirer.
            secret_id: ID of the Juju secret holding the access token.
            service_account_urn: URN of the DataHub service account.
        """
        if not self.charm.unit.is_leader():
            return

        relation.data[self.charm.app].update(
            {
                GMS_URL_FIELD: gms_url,
                SECRET_ID_FIELD: secret_id,
                SERVICE_ACCOUNT_URN_FIELD: service_account_urn,
            }
        )
        logger.info("Published datahub-client connection on relation %s", relation.id)


class DatahubClientRequirer(Object):
    """Requirer side of the datahub_client relation."""

    def __init__(self, charm: CharmBase, relation_name: str = DEFAULT_RELATION_NAME):
        """Construct.

        Args:
            charm: The charm instance.
            relation_name: Name of the relation.
        """
        super().__init__(charm, relation_name)
        self.charm = charm
        self.relation_name = relation_name

    @property
    def is_related(self) -> bool:
        """Return whether the relation currently exists."""
        return bool(self.charm.model.relations.get(self.relation_name))

    def get_connection(self) -> Optional[DatahubConnection]:
        """Return the current DataHub connection details.

        Returns:
            A DatahubConnection, or None while the provider has not published
            complete details or the token secret is not readable yet.
        """
        relations = self.charm.model.relations.get(self.relation_name, [])
        if not relations:
            return None

        relation = relations[0]
        if not relation.app:
            return None

        data = relation.data[relation.app]
        gms_url = data.get(GMS_URL_FIELD)
        secret_id = data.get(SECRET_ID_FIELD)
        service_account_urn = data.get(SERVICE_ACCOUNT_URN_FIELD)
        if not all([gms_url, secret_id, service_account_urn]):
            logger.debug("datahub-client relation data is incomplete")
            return None

        try:
            secret = self.charm.model.get_secret(id=secret_id)
            token = secret.get_content(refresh=True).get(TOKEN_SECRET_KEY)
        except SecretNotFoundError:
            logger.info("datahub-client token secret '%s' not found yet", secret_id)
            return None
        except ModelError as e:
            logger.info("datahub-client token secret '%s' is not readable yet: %s", secret_id, e)
            return None

        if not token:
            logger.warning("datahub-client token secret '%s' carries no usable '%s'", secret_id, TOKEN_SECRET_KEY)
            return None

        return DatahubConnection(
            gms_url=str(gms_url),
            token=token,
            service_account_urn=str(service_account_urn),
        )
