import asyncio
import os
import pathlib
import socket

import click
from datasette import hookimpl

# tailscale-rs requires this env var to be set to acknowledge that it is
# experimental software. We set it on behalf of the user (and warn them) so
# that `datasette tailscale` works out of the box.
EXPERIMENT_VAR = "TS_RS_EXPERIMENT"
EXPERIMENT_VALUE = "this_is_unstable_software"

# datasette serve options that we override or that don't make sense when
# serving over a tailnet. Everything NOT in this set is inherited verbatim from
# datasette serve - so new serve options are picked up automatically.
OVERRIDDEN_SERVE_OPTIONS = {
    "host",  # forced to 127.0.0.1
    "port",  # forced to an OS-assigned free loopback port
    "uds",  # we bind host/port, not a unix socket
    "reload",  # hupper re-exec would re-run the tailnet connect on every change
    "get",  # one-shot request-and-exit makes no sense for a long-lived listener
    "open_browser",  # would open a useless loopback URL
    "ssl_keyfile",  # we reverse-proxy plain HTTP; the tailnet encrypts the wire
    "ssl_certfile",
}


def _free_loopback_port():
    "Ask the OS for a free port on 127.0.0.1."
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _default_state_dir():
    "XDG state directory for persisting tailnet node identity."
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
        "~/.local/state"
    )
    return pathlib.Path(base) / "datasette-tailscale"


@hookimpl
def register_commands(cli):
    # Import here so importing the plugin never fails if datasette internals move
    from datasette.cli import serve

    # Inherit every serve option except the ones we override. This means future
    # additions to `datasette serve` are picked up automatically without any
    # changes to this plugin.
    inherited = [
        param
        for param in serve.params
        if param.name not in OVERRIDDEN_SERVE_OPTIONS
    ]

    # Our own tailscale-specific options, shown first in --help.
    tailscale_options = [
        click.Option(
            ["--ts-hostname", "ts_hostname"],
            default="datasette",
            show_default=True,
            help="Hostname for this node on your tailnet",
        ),
        click.Option(
            ["--ts-authkey", "ts_authkey"],
            envvar="TS_AUTHKEY",
            default=None,
            help=(
                "Tailscale auth key (or set TS_AUTHKEY). If omitted, an "
                "interactive login URL is printed on first run."
            ),
        ),
        click.Option(
            ["--ts-state-dir", "ts_state_dir"],
            type=click.Path(file_okay=False, dir_okay=True),
            default=None,
            help=(
                "Directory for persisting tailnet node identity "
                "(default: $XDG_STATE_HOME/datasette-tailscale)"
            ),
        ),
        click.Option(
            ["--ts-port", "ts_port"],
            type=click.IntRange(1, 65535),
            default=80,
            show_default=True,
            help="Port to listen on over the tailnet",
        ),
    ]

    def tailscale(ts_hostname, ts_authkey, ts_state_dir, ts_port, **serve_kwargs):
        # Force Datasette to bind to loopback only - the tailnet listener is the
        # sole ingress. Supply values for every serve option we suppressed so
        # the serve callback's full signature is satisfied.
        free_port = _free_loopback_port()
        serve_kwargs.update(
            host="127.0.0.1",
            port=free_port,
            uds=None,
            reload=False,
            get=None,
            open_browser=False,
            ssl_keyfile=None,
            ssl_certfile=None,
        )

        # Build the Datasette instance using serve's own logic (file validation,
        # --create, config_dir handling, metadata parsing, settings, etc.) but
        # stop short of running uvicorn.
        ds = serve.callback(**serve_kwargs, return_instance=True)

        if ts_state_dir:
            state_path = pathlib.Path(ts_state_dir)
        else:
            state_path = _default_state_dir()
        state_path.mkdir(parents=True, exist_ok=True)
        key_file = str(state_path / "{}.json".format(ts_hostname))

        asyncio.run(
            _run(ds, free_port, ts_hostname, ts_authkey, key_file, ts_port)
        )

    cmd = click.Command(
        name="tailscale",
        callback=tailscale,
        params=tailscale_options + inherited,
        help=(
            "Serve Datasette over your Tailscale tailnet.\n\n"
            "Datasette binds to 127.0.0.1 only; a userspace Tailscale node is "
            "the sole ingress, so the instance is reachable only by other "
            "devices on your tailnet. Accepts all the options that "
            "`datasette serve` accepts."
        ),
    )
    cli.add_command(cmd)


async def _proxy(ts_stream, port):
    "Bridge one tailnet TCP stream to a fresh loopback connection to Datasette."
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
    except OSError:
        return

    # Disable Nagle on the loopback socket. Without this, small HTTP writes
    # (headers, the tail of a response) can sit unflushed waiting for an ACK,
    # which shows up as responses dribbling through or appearing to hang.
    sock = writer.get_extra_info("socket")
    if sock is not None:
        try:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            pass

    async def tailnet_to_local():
        try:
            while True:
                data = await ts_stream.recv()
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        # Half-close: tell the backend we're done sending, but keep the socket
        # open so it can still write its response back to us. Fully closing here
        # would race the response and truncate it.
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass

    async def local_to_tailnet():
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                # send() may transmit fewer bytes than offered.
                while data:
                    sent = await ts_stream.send(data)
                    data = data[sent:]
        except Exception:
            pass

    try:
        await asyncio.gather(tailnet_to_local(), local_to_tailnet())
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _run(ds, free_port, ts_hostname, authkey, key_file, ts_port):
    import uvicorn

    os.environ.setdefault(EXPERIMENT_VAR, EXPERIMENT_VALUE)
    import tailscale

    click.echo(
        "datasette-tailscale uses tailscale-rs, which is experimental software "
        "with unvalidated cryptography. Use only on a tailnet you trust.",
        err=True,
    )

    # Run Datasette's startup plugin hooks, as `datasette serve` would.
    await ds.invoke_startup()

    # Start Datasette on loopback in this event loop.
    config = uvicorn.Config(
        ds.app(),
        host="127.0.0.1",
        port=free_port,
        log_level="info",
        lifespan="on",
        # The tailnet client controls connection lifetime; keep the loopback
        # connection alive long enough that the backend doesn't unilaterally
        # tear down a keep-alive connection the client is still reusing.
        timeout_keep_alive=120,
    )
    server = uvicorn.Server(config)
    uvicorn_task = asyncio.create_task(server.serve())

    # Join the tailnet.
    click.echo("Connecting to your tailnet as {!r}...".format(ts_hostname))
    if not authkey:
        click.echo(
            "No auth key supplied - watch for an interactive login URL below.",
            err=True,
        )
    dev = await tailscale.connect(key_file, authkey, hostname=ts_hostname)
    ipv4 = await dev.ipv4_addr()
    click.echo("Connected. Tailnet IPv4: {}".format(ipv4))

    listener = await dev.tcp_listen((ipv4, ts_port))
    suffix = "" if ts_port == 80 else ":{}".format(ts_port)
    click.echo(
        "Serving Datasette at http://{}{} (reachable only on your tailnet)".format(
            ts_hostname, suffix
        )
    )
    click.echo("Press Ctrl-C to stop.")

    try:
        while True:
            stream = await listener.accept()
            asyncio.create_task(_proxy(stream, free_port))
    finally:
        server.should_exit = True
        await uvicorn_task
