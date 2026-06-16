import asyncio

import pytest
from click.testing import CliRunner
from datasette.app import Datasette
from datasette.cli import cli, serve

from datasette_tailscale import (
    OVERRIDDEN_SERVE_OPTIONS,
    _proxy,
    register_commands,
)


@pytest.mark.asyncio
async def test_plugin_is_installed():
    datasette = Datasette(memory=True)
    response = await datasette.client.get("/-/plugins.json")
    assert response.status_code == 200
    installed_plugins = {p["name"] for p in response.json()}
    assert "datasette-tailscale" in installed_plugins


def _build_tailscale_command():
    captured = {}

    class FakeCli:
        def add_command(self, cmd):
            captured["cmd"] = cmd

    register_commands(FakeCli())
    return captured["cmd"]


def test_command_registered():
    result = CliRunner().invoke(cli, ["tailscale", "--help"])
    assert result.exit_code == 0
    assert "Serve Datasette over your Tailscale tailnet" in result.output


def test_inherits_serve_options_minus_overrides():
    cmd = _build_tailscale_command()
    names = {p.name for p in cmd.params}

    # Our own options are present
    assert {"ts_hostname", "ts_authkey", "ts_state_dir", "ts_port"} <= names

    # Overridden serve options are NOT exposed
    assert not (names & OVERRIDDEN_SERVE_OPTIONS)

    # Every other serve option IS inherited automatically
    serve_names = {p.name for p in serve.params}
    expected_inherited = serve_names - OVERRIDDEN_SERVE_OPTIONS
    assert expected_inherited <= names


def test_no_host_or_port_options_exposed():
    cmd = _build_tailscale_command()
    all_opts = [opt for p in cmd.params for opt in getattr(p, "opts", [])]
    assert "--host" not in all_opts
    assert "--port" not in all_opts
    assert "--reload" not in all_opts
    assert "--ssl-keyfile" not in all_opts


def test_our_options_use_ts_prefix():
    # Our options are namespaced under --ts-* so they can never clash with a
    # future `datasette serve` option that we inherit dynamically.
    cmd = _build_tailscale_command()
    our_names = {"ts_hostname", "ts_authkey", "ts_state_dir", "ts_port"}
    for param in cmd.params:
        if param.name in our_names:
            assert all(
                opt.startswith("--ts-") for opt in param.opts
            ), param.opts


class FakeTcpStream:
    """Mimics tailscale.TcpStream backed by an in-memory queue.

    recv() yields chunks the test pushes in; send() records outgoing bytes.
    """

    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = bytearray()

    async def recv(self):
        await asyncio.sleep(0)
        if self._incoming:
            return self._incoming.pop(0)
        return b""  # EOF

    async def send(self, data):
        # Exercise the partial-write loop: only send half each call.
        n = max(1, len(data) // 2)
        self.sent.extend(data[:n])
        await asyncio.sleep(0)
        return n


@pytest.mark.asyncio
async def test_proxy_bridges_to_loopback():
    # A tiny loopback TCP server that upper-cases whatever it receives,
    # standing in for Datasette.
    received = bytearray()

    async def handle(reader, writer):
        data = await reader.read(1024)
        received.extend(data)
        writer.write(data.upper())
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]

    async with server:
        ts_stream = FakeTcpStream([b"hello ", b"tailnet"])
        await _proxy(ts_stream, port)

    # The loopback backend saw the bytes the tailnet stream produced...
    assert bytes(received) == b"hello tailnet"
    # ...and the upper-cased response made it back out to the tailnet stream.
    assert bytes(ts_stream.sent) == b"HELLO TAILNET"


@pytest.mark.asyncio
async def test_proxy_handles_unreachable_backend():
    # Port 1 on loopback should refuse the connection; _proxy must not raise.
    ts_stream = FakeTcpStream([b"data"])
    await _proxy(ts_stream, 1)
    assert bytes(ts_stream.sent) == b""
