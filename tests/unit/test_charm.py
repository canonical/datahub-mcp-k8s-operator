# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Unit tests for the charm's reconcile and status logic."""

import dataclasses
import json

import ops
import pytest
from ops import testing

import literals
from tests.unit.conftest import GMS_URL


def _layer(state):
    """Return the pebble layer the charm planned for the workload container."""
    container = state.get_container(literals.CONTAINER_NAME)
    return container.layers[literals.SERVICE_NAME]


def _service_env(state):
    """Return the workload environment the charm planned."""
    return _layer(state).services[literals.SERVICE_NAME].environment


def _check_info(status):
    """Return a CheckInfo matching the check the charm plans."""
    return testing.CheckInfo(
        "up",
        status=status,
        threshold=literals.HEALTHCHECK_FAILURE_THRESHOLD,
    )


def _drifted_layer():
    """Return a plan that keeps the check but runs the wrong command."""
    return ops.pebble.Layer(
        {
            "services": {literals.SERVICE_NAME: {"override": "replace", "command": "sleep infinity"}},
            "checks": {
                "up": {
                    "override": "replace",
                    "period": "10s",
                    "threshold": literals.HEALTHCHECK_FAILURE_THRESHOLD,
                    "http": {"url": f"http://localhost:{literals.MCP_PORT}{literals.HEALTH_PATH}"},
                }
            },
        }
    )


class TestBlockedStates:
    """Tests for the conditions that keep the charm out of Active."""

    def test_blocks_without_datahub_relation(self, charm_ctx, container):
        """Without the DataHub relation there is no catalog to serve."""
        state = testing.State(leader=True, containers={container})

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert isinstance(out.unit_status, ops.BlockedStatus)
        assert literals.DATAHUB_RELATION_NAME in out.unit_status.message

    def test_blocks_until_datahub_publishes_the_token(self, charm_ctx, container):
        """A related-but-empty databag is a wait, not a failure."""
        relation = testing.Relation(
            endpoint=literals.DATAHUB_RELATION_NAME,
            remote_app_name="datahub-k8s",
        )
        state = testing.State(leader=True, relations={relation}, containers={container})

        out = charm_ctx.run(charm_ctx.on.relation_changed(relation), state)

        assert isinstance(out.unit_status, ops.BlockedStatus)
        assert "access token" in out.unit_status.message

    def test_blocks_when_oauth_is_enabled_without_ingress(self, charm_ctx, base_state):
        """Clients cannot discover the authorization server without a public URL."""
        oauth = testing.Relation(endpoint=literals.OAUTH_RELATION_NAME, remote_app_name="idp")
        state = dataclasses.replace(base_state, relations=base_state.relations | {oauth})

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert isinstance(out.unit_status, ops.BlockedStatus)
        assert "ingress" in out.unit_status.message

    def test_blocks_when_oauth_is_enabled_over_http(self, charm_ctx, base_state):
        """An http issuer is rejected by the workload, which would otherwise restart forever."""
        oauth = testing.Relation(endpoint=literals.OAUTH_RELATION_NAME, remote_app_name="idp")
        ingress = testing.Relation(
            endpoint=literals.INGRESS_RELATION_NAME,
            remote_app_name="nginx-ingress-integrator",
            remote_app_data={"ingress": json.dumps({"url": "http://mcp.example.com"})},
        )
        state = dataclasses.replace(base_state, relations=base_state.relations | {oauth, ingress})

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert isinstance(out.unit_status, ops.BlockedStatus)
        assert "https" in out.unit_status.message


class TestReconcile:
    """Tests for the happy path."""

    def test_becomes_active_and_plans_the_workload(self, charm_ctx, base_state):
        """A published relation is enough to run the server."""
        out = charm_ctx.run(charm_ctx.on.config_changed(), base_state)

        assert out.unit_status == ops.ActiveStatus()
        service = _layer(out).services[literals.SERVICE_NAME]
        assert literals.SERVER_ENTRYPOINT in service.command
        assert service.startup == "enabled"

    def test_passes_datahub_credentials_to_the_workload(self, charm_ctx, base_state):
        """The GMS URL and token come from the relation, not from config."""
        out = charm_ctx.run(charm_ctx.on.config_changed(), base_state)

        env = _service_env(out)
        assert env["DATAHUB_GMS_URL"] == GMS_URL
        assert env["DATAHUB_GMS_TOKEN"] == "pat"  # nosec B105

    def test_mutation_tools_are_disabled_by_default(self, charm_ctx, base_state):
        """Read-only is the default tool surface."""
        out = charm_ctx.run(charm_ctx.on.config_changed(), base_state)

        assert _service_env(out)["TOOLS_IS_MUTATION_ENABLED"] == "false"

    def test_mutation_tools_follow_the_config_option(self, charm_ctx, base_state):
        """Enabling mutation tools is an explicit, per-deployment decision."""
        state = dataclasses.replace(base_state, config={"enable-mutation-tools": True})

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert _service_env(out)["TOOLS_IS_MUTATION_ENABLED"] == "true"

    def test_client_authentication_is_off_without_an_oauth_relation(self, charm_ctx, base_state):
        """No oauth relation means no token verifier in the workload."""
        out = charm_ctx.run(charm_ctx.on.config_changed(), base_state)

        assert "MCP_AUTH_INTROSPECTION_URL" not in _service_env(out)

    def test_opens_the_workload_port(self, charm_ctx, base_state):
        """The MCP port is opened so the ingress can reach it."""
        out = charm_ctx.run(charm_ctx.on.config_changed(), base_state)

        assert testing.TCPPort(literals.MCP_PORT) in out.opened_ports

    def test_waits_for_the_workload_container(self, charm_ctx, base_state):
        """An unreachable container is a wait, not a block."""
        container = testing.Container(name=literals.CONTAINER_NAME, can_connect=False)
        state = dataclasses.replace(base_state, containers={container})

        out = charm_ctx.run(charm_ctx.on.config_changed(), state)

        assert isinstance(out.unit_status, ops.MaintenanceStatus)


class TestUpdateStatus:
    """Tests for the health reporting loop."""

    @pytest.mark.parametrize(
        "check_status,expected",
        [
            (ops.pebble.CheckStatus.UP, ops.ActiveStatus()),
            (ops.pebble.CheckStatus.DOWN, ops.MaintenanceStatus("status check: DOWN")),
        ],
    )
    def test_reports_the_workload_check(self, charm_ctx, base_state, check_status, expected):
        """A failing `up` check surfaces as maintenance, not as Active."""
        # Bring the container up to the plan the charm would have written.
        planned = charm_ctx.run(charm_ctx.on.config_changed(), base_state)
        layer = _layer(planned)
        container = testing.Container(
            name=literals.CONTAINER_NAME,
            can_connect=True,
            layers={literals.SERVICE_NAME: layer},
            service_statuses={literals.SERVICE_NAME: ops.pebble.ServiceStatus.ACTIVE},
            check_infos={_check_info(check_status)},
        )
        state = dataclasses.replace(base_state, containers={container})

        out = charm_ctx.run(charm_ctx.on.update_status(), state)

        assert out.unit_status == expected

    def test_replans_when_the_plan_drifted(self, charm_ctx, base_state):
        """A container whose plan does not match the charm's intent is replanned."""
        container = testing.Container(
            name=literals.CONTAINER_NAME,
            can_connect=True,
            layers={literals.SERVICE_NAME: _drifted_layer()},
            check_infos={_check_info(ops.pebble.CheckStatus.UP)},
        )
        state = dataclasses.replace(base_state, containers={container})

        out = charm_ctx.run(charm_ctx.on.update_status(), state)

        assert out.unit_status == ops.ActiveStatus()
        assert literals.SERVER_ENTRYPOINT in _layer(out).services[literals.SERVICE_NAME].command

    def test_stale_ingress_address_is_corrected_while_the_workload_is_unreachable(self, charm_ctx, base_state):
        """A rescheduled pod's address is republished before the health checks bail out."""
        ingress = testing.Relation(
            endpoint=literals.INGRESS_RELATION_NAME,
            remote_app_name="nginx-ingress-integrator",
            local_unit_data={"host": '"datahub-mcp-k8s-0"', "ip": '"10.0.0.1"'},
        )
        container = testing.Container(name=literals.CONTAINER_NAME, can_connect=False)
        state = dataclasses.replace(
            base_state,
            relations=base_state.relations | {ingress},
            containers={container},
            networks={
                testing.Network(literals.INGRESS_RELATION_NAME, [testing.BindAddress([testing.Address("10.0.0.2")])])
            },
        )

        out = charm_ctx.run(charm_ctx.on.update_status(), state)

        assert isinstance(out.unit_status, ops.MaintenanceStatus)
        rel_out = out.get_relation(ingress.id)
        assert json.loads(rel_out.local_unit_data["ip"]) == "10.0.0.2"
        assert json.loads(rel_out.local_app_data["port"]) == literals.MCP_PORT
