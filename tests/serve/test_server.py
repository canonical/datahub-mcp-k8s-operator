# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Tests that boot the real entrypoint against a stub DataHub.

These cover what unit tests cannot: that the server actually starts, serves the
routes the charm and its clients depend on, and enforces authentication when the
charm configures it. Nothing here needs Juju or a DataHub deployment.
"""

import json
import re

import requests

from tests.serve.conftest import ISSUER

READ_ONLY_TOOLS = {
    "search",
    "get_lineage",
    "get_entities",
    "list_schema_fields",
    "get_dataset_queries",
    "get_lineage_paths_between",
}


def _mcp_call(base_url: str, method: str, token: str = "") -> requests.Response:  # nosec B107
    """Issue one JSON-RPC call against the streamable HTTP transport.

    Args:
        base_url: Base URL of the MCP server.
        method: JSON-RPC method name.
        token: Bearer token to present, if any.

    Returns:
        The raw HTTP response.
    """
    headers = {"Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return requests.post(
        f"{base_url}/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": {}},
        headers=headers,
        timeout=60,
    )


def _tool_names(base_url: str) -> set:
    """Return the names of the tools the server advertises.

    Args:
        base_url: Base URL of the MCP server.

    Returns:
        The advertised tool names.

    Raises:
        AssertionError: If the response carries no JSON-RPC payload.
    """
    response = _mcp_call(base_url, "tools/list")
    response.raise_for_status()
    for line in response.text.splitlines():
        if line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
            return {tool["name"] for tool in payload["result"]["tools"]}
    raise AssertionError(f"no JSON-RPC payload in response: {response.text[:200]}")


class TestWithoutAuthentication:
    """Tests for the endpoint the charm serves before an oauth relation exists."""

    def test_serves_the_health_route(self, mcp_server):
        """The route backing the pebble check answers, so the unit can go active."""
        response = requests.get(f"{mcp_server()}/health", timeout=30)

        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_serves_the_read_only_tool_set(self, mcp_server):
        """Mutation tools stay off until a deployment opts in."""
        assert _tool_names(mcp_server()) == READ_ONLY_TOOLS

    def test_mutation_tools_follow_the_charm_config(self, mcp_server):
        """`enable-mutation-tools` reaches the workload through this variable."""
        tools = _tool_names(mcp_server(TOOLS_IS_MUTATION_ENABLED="true"))

        assert tools > READ_ONLY_TOOLS

    def test_answers_without_a_token(self, mcp_server):
        """With no oauth relation the endpoint relies on whatever fronts it."""
        assert _mcp_call(mcp_server(), "tools/list").status_code == 200


class TestWithAuthentication:
    """Tests for the endpoint once the charm has configured an oauth relation."""

    def test_rejects_a_call_carrying_no_token(self, mcp_server):
        """Configuring oauth must actually close the endpoint."""
        response = _mcp_call(mcp_server(auth=True), "tools/list")

        assert response.status_code == 401

    def test_rejects_a_token_the_provider_will_not_confirm(self, mcp_server):
        """The provider is unreachable here, which must fail closed."""
        response = _mcp_call(mcp_server(auth=True), "tools/list", token="not-a-real-token")  # nosec B106

        assert response.status_code == 401

    def test_points_clients_at_the_metadata_after_a_401(self, mcp_server):
        """Without this header a client has no way to discover where to get a token."""
        response = _mcp_call(mcp_server(auth=True), "tools/list")

        assert "resource_metadata" in response.headers.get("WWW-Authenticate", "")

    def test_publishes_the_protected_resource_metadata(self, mcp_server):
        """The document the header points at has to exist and name the provider."""
        response = _mcp_call(mcp_server(auth=True), "tools/list")
        metadata_url = re.search(r'resource_metadata="([^"]+)"', response.headers["WWW-Authenticate"]).group(1)

        document = requests.get(metadata_url, timeout=30).json()

        assert [server.rstrip("/") for server in document["authorization_servers"]] == [ISSUER]

    def test_leaves_the_health_route_open(self, mcp_server):
        """The pebble check must keep working once the transport is protected."""
        response = requests.get(f"{mcp_server(auth=True)}/health", timeout=30)

        assert response.status_code == 200
