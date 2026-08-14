# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Literals."""

CONTAINER_NAME = "mcp-server"
SERVICE_NAME = "mcp-server"

# FastMCP defaults: the streamable HTTP transport is served at /mcp and the
# upstream server adds an unauthenticated /health route next to it.
MCP_PORT = 8000
MCP_PATH = "/mcp"
HEALTH_PATH = "/health"

# Consecutive `up`-check failures before pebble restarts the workload. At the
# 10s check period this is ~1 min: the server is a Python process that starts in
# seconds, so a minute of failures means it is genuinely stuck.
HEALTHCHECK_FAILURE_THRESHOLD = 6

# Paths baked into the rock (see rock/rockcraft.yaml).
PYTHON_BIN_PATH = "/usr/bin/python3.12"
SERVER_ENTRYPOINT = "/opt/mcp/serve.py"
SERVER_LIB_PATH = "/opt/mcp/lib"

DATAHUB_RELATION_NAME = "datahub-client"
INGRESS_RELATION_NAME = "ingress"

# OAuth 2.1 client authentication at the MCP endpoint. The MCP server acts as a
# resource server: it verifies bearer tokens the client obtained from the IdP.
OAUTH_RELATION_NAME = "oauth"
OAUTH_SCOPE = "openid profile email"
OAUTH_GRANT_TYPES = ["authorization_code"]
# The redirect URI is only published so the provider accepts the client
# registration; the MCP server never runs a browser redirect flow itself.
OAUTH_CALLBACK_PATH = "/oauth/callback"
