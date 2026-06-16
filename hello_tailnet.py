"""Smoke test: join the tailnet and serve a hello-world HTTP response.

Run with:
    TS_RS_EXPERIMENT=this_is_unstable_software \
        uv run --with tailscale-py python hello_tailnet.py
"""

import asyncio
import datetime
import tailscale

HOSTNAME = "datasette-tailscale"
PORT = 80


async def handle(stream):
    # Read the (single) HTTP request - we don't care about its contents.
    try:
        await stream.recv()
    except Exception:
        pass
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    peer = stream.remote_addr()
    body = f"Hello from {HOSTNAME} on the tailnet!\nYou are {peer[0]}:{peer[1]}\n{now}\n"
    body_bytes = body.encode()
    response = (
        b"HTTP/1.1 200 OK\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Length: " + str(len(body_bytes)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n" + body_bytes
    )
    # send() may not write everything in one call - loop until drained.
    while response:
        sent = await stream.send(response)
        response = response[sent:]
    print(f"served request from {peer[0]}:{peer[1]}")


async def main():
    with open(".ts-authkey") as fp:
        auth_key = fp.read().strip()

    print("connecting to tailnet...")
    dev = await tailscale.connect(
        ".ts-state.json", auth_key, hostname=HOSTNAME
    )
    ipv4 = await dev.ipv4_addr()
    ipv6 = await dev.ipv6_addr()
    print(f"connected! tailnet IPv4: {ipv4}  IPv6: {ipv6}")

    listener = await dev.tcp_listen((ipv4, PORT))
    print(f"listening on http://{ipv4}:{PORT}  (and http://{HOSTNAME}/)")
    print("hit it from another device on your tailnet. Ctrl-C to stop.")

    while True:
        stream = await listener.accept()
        asyncio.create_task(handle(stream))


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nshutting down")
