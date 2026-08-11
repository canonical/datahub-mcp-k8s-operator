# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Charm exceptions."""


class UnreadyStateError(Exception):
    """Raised when the charm cannot yet reconcile to a running workload."""
