# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Module for a Pydantic model that is used for the charm configuration."""

from charms.data_platform_libs.v0.data_models import BaseConfigModel


class CharmConfig(BaseConfigModel):
    """Typed configuration for the charm.

    Attributes:
        enable_mutation_tools: Expose the DataHub mutation tools alongside the
            read-only tool set.
    """

    enable_mutation_tools: bool = False
