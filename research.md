This is a fun systems design problem. Let me verify a couple of the key technical anchors (the relevant Datasette plugin hook and the current Tailscale embedding story) so the design rests on real APIs rather than my memory.Good — `register_commands(cli)` is confirmed. Now the Tailscale side.Good — both halves of this are real and well-supported, so the design can rest on actual APIs. Here's a proposal.

## The key architectural decision: `tsnet`, not bundled `tailscaled`

The instinct to "bundle the Go binary" is right, but there are two very different ways to do it, and one is much cleaner for this goal.

The heavyweight option is to ship `tailscaled` + `tailscale` and run the daemon. The problem is your requirement — *inbound* connections restricted to the tailnet. To accept inbound traffic on a tailnet IP the normal way, `tailscaled` wants a kernel TUN device, which needs root and joins the *whole machine* to your tailnet. Userspace networking mode (`tailscaled --tun=userspace-networking`) avoids the TUN device, but it's oriented around giving your app outbound access via a SOCKS5/HTTP proxy — it's awkward for the "expose one listener, inbound-only" case.

The right tool is `tsnet`. It embeds a Tailscale node directly into a Go program, joining a tailnet and accepting connections without running a separate `tailscaled` daemon or any system-level configuration, using a userspace gVisor TCP/IP stack — so no root privileges, no system daemons, and the node's identity/state live in a directory you control. Crucially, tsnet presents the application as its own device on the tailnet, so you can apply ACLs to the application rather than the host machine. That maps exactly onto "this Datasette is its own thing on my tailnet."

So the shape is: a small bundled Go binary embeds tsnet, joins the tailnet as its own node (e.g. hostname `datasette`), and reverse-proxies inbound tailnet traffic to a Datasette instance bound only to loopback. tsnet has a first-class pattern for this — an example in the package shows advertising a service backed by `httputil.NewSingleHostReverseProxy` pointed at an external address, served over the tailnet listener with HTTPS.

```
tailnet client ──TLS──▶ [tsnet node "datasette"]  ──HTTP──▶ 127.0.0.1:<port>  (datasette serve)
     (your laptop)         bundled Go proxy binary            loopback-only
```

The security invariant that makes the whole thing work: **Datasette binds to `127.0.0.1` only, never `0.0.0.0`.** The tsnet node is the sole ingress. If Datasette bound to a public/LAN interface, Tailscale would be irrelevant — anyone on the LAN could reach it.

## Plugin integration: `register_commands`

Datasette gives you exactly the hook you need. The `register_commands()` plugin hook (added in Datasette 0.59) lets a plugin register additional `datasette` CLI subcommands. Plugins using it must be `pip install`ed into the same virtualenv as Datasette rather than loaded via `--plugins-dir` — which is fine here since you're shipping a wheel anyway.

So the package (call it `datasette-tailscale`) registers a subcommand:

```python
from datasette import hookimpl
import click

@hookimpl
def register_commands(cli):
    @cli.command(name="tailscale")
    @click.argument("files", nargs=-1, type=click.Path(exists=True))
    @click.option("--hostname", default="datasette")
    @click.option("--authkey", envvar="TS_AUTHKEY", default=None)
    @click.option("--state-dir", default=None)
    @click.option("--https/--no-https", default=True)
    @click.option("--funnel", is_flag=True, default=False)
    # ...plus pass-through of normal serve options (--metadata, --setting, etc.)
    def tailscale(files, hostname, authkey, state_dir, https, funnel, ...):
        "Serve Datasette reachable only over your Tailnet"
        ...
```

Usage becomes:

```
pip install datasette-tailscale
datasette tailscale mydata.db
# first run prints an auth URL; then:
# Serving at https://datasette.your-tailnet.ts.net
```

The command body:

1. Picks a free loopback port.
2. Spawns the bundled Go proxy as a subprocess: `datasette-tsnet-proxy --hostname … --target http://127.0.0.1:<port> [--https] [--funnel] [--authkey …] [--state-dir …]`.
3. Streams the proxy's stdout so the auth URL / "listening at …" line surfaces to the user.
4. Starts Datasette's own serve path with host forced to `127.0.0.1` and the chosen port (delegating to Datasette's serve internals, just overriding host/port and ignoring any user-supplied `--host`/`--port` — or erroring loudly if they try to set a public host).
5. On shutdown, signals the proxy to exit cleanly so the node deregisters.

The proxy should retry the backend connection so startup ordering between the two processes doesn't matter.

## Packaging the binary

This is the fiddly part you flagged. A static Go binary cross-compiles trivially (`CGO_ENABLED=0`), and the userspace stack is portable, so build a matrix in CI — linux x86_64/arm64, macOS arm64/x86_64, Windows — and ship **per-platform wheels** with the right binary embedded as package data. Use a cibuildwheel-style matrix so `pip` selects the correct wheel via its platform tag. Two real caveats:

- **Size.** A tsnet binary pulls in the whole Tailscale stack — expect tens of MB per platform. If that bloats the wheel uncomfortably, the alternative is a small pure-Python wheel that fetches the right binary on first run (Playwright-style), with a pinned version and a checksum to verify integrity. Trades wheel size for a network dependency at first launch.
- **Code signing.** An unsigned bundled binary will trip macOS Gatekeeper and some Windows AV. For a smooth install you'll eventually want to notarize/sign the binaries in CI.

Avoid the temptation of an sdist that compiles Go at install time — that forces a Go toolchain onto the user.

## Auth and state

tsnet supports two auth modes; expose both:

- **Interactive** — on first run tsnet emits an auth URL; the user clicks it to attach the node. Good for desktops.
- **Auth key** — `TS_AUTHKEY` / `--authkey`, non-interactive, for headless servers. Reusable + ephemeral keys are the usual choice, so nodes can be (re)created and disappear when the program isn't running.

Make the state directory configurable (default to an XDG path like `~/.local/state/datasette-tailscale`). A persistent state dir gives a stable node identity across restarts (right for a home server); ephemeral mode (the node vanishes on exit) suits short-lived sessions. Let the user choose.

## A worthwhile extension: identity-aware access

"Only on my tailnet" is the baseline. A natural step further: have the Go proxy call Tailscale's `WhoIs` on each connection and inject the caller's identity as headers (login, display name) before forwarding to Datasette. A companion Datasette auth plugin then reads those headers and grants per-user permissions. This is the same idea as the existing `datasette-auth-tailscale` plugin, but self-contained in your bundle. Now Datasette doesn't just know the request came from the tailnet — it knows *which* tailnet user it is, so you get real per-user actor permissions for free. Worth shipping as an opt-in `--tailscale-auth` flag.

Separately, a `--funnel` flag could expose the node publicly via Tailscale Funnel — but that breaks the core guarantee, so keep it off by default and warn loudly when it's set.

## Tradeoffs to be honest about

The biggest downside of bundling is that **you own Tailscale's patch cadence.** With system `tailscaled`, Tailscale ships security fixes and the OS updates independently. Bundle it and a Tailscale CVE means you have to cut a new wheel and users have to upgrade. Pin the `tailscale.com` version you build against, watch their releases, and document the update story. (Version pinning also matters because tsnet/Tailscale behavior has shifted across versions.)

Other things to plan for: clean shutdown so ephemeral nodes deregister; binding Datasette to an OS-assigned free loopback port to dodge clashes; HTTPS via the tailnet cert (you get `https://datasette.<tailnet>.ts.net` essentially for free through tsnet's TLS listener, which is nice since browsers increasingly assume HTTPS); and graceful handling of headless first-run where no browser is available to complete interactive auth (hence the authkey path).

If it'd help, I can sketch the actual `main.go` for the proxy (tsnet `ListenService` + reverse proxy + `WhoIs` header injection) or the CI matrix for building the per-platform wheels.