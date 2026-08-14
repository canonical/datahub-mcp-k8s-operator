# Copyright 2026 Canonical Ltd.
# See LICENSE file for licensing details.

"""Fixtures for the rock entrypoint tests."""

import http.server
import json
import os
import socket
import subprocess  # nosec B404
import sys
import threading
import time
from pathlib import Path

import pytest
import requests

SERVE_DIR = Path(__file__).parents[2] / "rock" / "files"
sys.path.insert(0, str(SERVE_DIR))

ISSUER = "https://idp.example.com"
STARTUP_TIMEOUT = 120

GMS_CONFIG = {
    "models": {},
    "versions": {"acryldata/datahub": {"version": "v1.4.0.5"}},
    "managedIngestion": {"enabled": False},
    "datahub": {"serverType": "prod"},
    "retention": {"supportsRetention": True},
    "statefulIngestionCapable": True,
    "patchCapable": True,
    "noCode": "true",
    "timeZone": "GMT",
}


def _free_port() -> int:
    """Return a port that is free right now.

    Returns:
        A free TCP port on the loopback interface.
    """
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _GmsHandler(http.server.BaseHTTPRequestHandler):
    """Answer the one GMS route the server needs in order to start."""

    def do_GET(self):  # noqa: N802
        """Serve /config, and nothing else."""
        if self.path.rstrip("/") != "/config":
            self.send_error(404)
            return
        body = json.dumps(GMS_CONFIG).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        """Keep the stub out of the test output.

        Args:
            args: The message the base class would have logged.
        """


@pytest.fixture(scope="session")
def stub_gms():
    """Run a stand-in for DataHub's GMS API.

    Yields:
        The base URL of the stub.
    """
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _GmsHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


def _wait_until_serving(process, base_url: str) -> None:
    """Block until the server answers, or explain why it never will.

    Args:
        process: The server process.
        base_url: Where the server should come up.

    Raises:
        AssertionError: If the server exits or never starts serving.
    """
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"server exited with {process.returncode}:\n{process.stdout.read()}")
        try:
            if requests.get(f"{base_url}/health", timeout=5).status_code == 200:
                return
        except requests.RequestException:
            time.sleep(1)
    raise AssertionError(f"server did not start within {STARTUP_TIMEOUT}s")


@pytest.fixture
def mcp_server(stub_gms):
    """Return a factory that boots the real entrypoint and returns its URL.

    Args:
        stub_gms: Base URL of the stub GMS API.

    Yields:
        A factory taking `auth` and any extra environment variables.
    """
    processes = []

    def _start(auth: bool = False, **extra_env) -> str:
        """Start the entrypoint and wait for it to serve.

        Args:
            auth: Whether to configure client authentication.
            extra_env: Extra environment for the server process.

        Returns:
            The base URL the server is listening on.
        """
        port = _free_port()
        base_url = f"http://127.0.0.1:{port}"
        env = {
            **os.environ,
            "PYTHONPATH": str(SERVE_DIR),
            "DATAHUB_GMS_URL": stub_gms,
            "DATAHUB_GMS_TOKEN": "stub-token",  # nosec B105
            "DATAHUB_TELEMETRY_ENABLED": "false",
            "FASTMCP_HOST": "127.0.0.1",
            "FASTMCP_PORT": str(port),
        }
        if auth:
            env.update(
                {
                    "MCP_AUTH_ISSUER": ISSUER,
                    "MCP_AUTH_INTROSPECTION_URL": f"{ISSUER}/admin/oauth2/introspect",
                    "MCP_AUTH_CLIENT_ID": "datahub-mcp",
                    "MCP_AUTH_CLIENT_SECRET": "s3cret",  # nosec B105
                    "MCP_AUTH_BASE_URL": base_url,
                }
            )
        env.update(extra_env)

        # Lifetime is managed by the fixture teardown below.
        process = subprocess.Popen(  # nosec B603  # pylint: disable=consider-using-with
            [sys.executable, str(SERVE_DIR / "serve.py")],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        processes.append(process)
        _wait_until_serving(process, base_url)
        return base_url

    yield _start

    for process in processes:
        process.terminate()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()


@pytest.fixture
def oauth_env(monkeypatch):
    """Return a helper that sets the workload's OAuth environment.

    Args:
        monkeypatch: The pytest monkeypatch fixture.

    Returns:
        A callable taking the variables to set, without their prefix.
    """

    def _set(**overrides):
        """Set the OAuth variables the charm would provide.

        Args:
            overrides: Variables to set, without the `MCP_AUTH_` prefix.
        """
        for name in (
            "MCP_AUTH_ISSUER",
            "MCP_AUTH_JWT_ACCESS_TOKEN",
            "MCP_AUTH_JWKS_URL",
            "MCP_AUTH_INTROSPECTION_URL",
            "MCP_AUTH_CLIENT_ID",
            "MCP_AUTH_CLIENT_SECRET",
            "MCP_AUTH_BASE_URL",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in overrides.items():
            monkeypatch.setenv(f"MCP_AUTH_{name.upper()}", value)

    return _set
