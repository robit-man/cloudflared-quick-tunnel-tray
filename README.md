# Cloudflared Quick Tunnel Tray

[![CI](https://github.com/robit-man/cloudflared-quick-tunnel-tray/actions/workflows/ci.yml/badge.svg)](https://github.com/robit-man/cloudflared-quick-tunnel-tray/actions/workflows/ci.yml)

A small Linux system service and tray indicator for creating disposable
[Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/).

The default tray action publishes the host's SSH service on port 22. You can
also select any listening local TCP port, cycle to a new random
`trycloudflare.com` endpoint, stop the tunnel, or copy the endpoint hostname.

## What it installs

- A hardened system service that brokers Quick Tunnels.
- One isolated tunnel slot per Unix UID, authenticated with `SO_PEERCRED`.
- A globally enabled systemd user service that starts the tray in graphical
  sessions and stops that user's tunnel when the tray session ends.
- A CLI for scripting and troubleshooting.

The root broker never runs `cloudflared` as root. Each tunnel child drops to
the Unix account that made the request. The broker only accepts loopback TCP
ports and never accepts an arbitrary destination host.

## Requirements

- Linux with systemd and a graphical user session
- Python 3.10 or newer
- `cloudflared`
- `setpriv` from util-linux
- GTK 3, PyGObject, Ayatana AppIndicator, and libnotify bindings

On Ubuntu, the GUI dependencies are normally available with:

```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7
```

Install `cloudflared` using Cloudflare's package instructions for your
distribution.

## Install

```bash
git clone https://github.com/robit-man/cloudflared-quick-tunnel-tray.git
cd cloudflared-quick-tunnel-tray
sudo ./install.sh
```

The installer starts the system broker immediately and enables the tray for
all users' future graphical sessions. When run through `sudo` inside an active
desktop session, it also starts the invoking user's tray immediately.

## Tray actions

- **Copy endpoint** — copies only the generated hostname.
- **Start / cycle SSH tunnel (port 22)** — replaces the current endpoint with
  a fresh SSH tunnel.
- **Forward custom TCP port…** — validates that the local port is listening,
  then creates a TCP Quick Tunnel.
- **Stop tunnel** — stops only the current Unix user's tunnel.
- **Quit indicator** — ends the tray service and stops that user's tunnel.

The tray status and controls are per Unix UID. Two logged-in users can have
independent endpoints, and neither can inspect or control the other's tunnel
through the service API.

## CLI

```bash
cloudflared-quick-tunnelctl status
cloudflared-quick-tunnelctl start --port 22
cloudflared-quick-tunnelctl cycle
cloudflared-quick-tunnelctl cycle --port 8080
cloudflared-quick-tunnelctl stop
```

Add `--json` before or after the subcommand for structured output.

To connect to an SSH Quick Tunnel from a machine with `cloudflared` installed:

```bash
ssh -o 'ProxyCommand=cloudflared access ssh --hostname %h' USER@ENDPOINT
```

For a general TCP service, create a local client-side listener first:

```bash
cloudflared access tcp --hostname ENDPOINT --url localhost:9000
```

Then point the application at `localhost:9000`.

## Security notes

Quick Tunnels create public Cloudflare endpoints and have no uptime guarantee.
SSH still requires SSH authentication, but a custom TCP service may not have
authentication of its own. Do not publish an unauthenticated administrative,
database, or development port.

The control socket is intentionally reachable by local users. The daemon uses
the kernel-provided peer UID, not a client-supplied username, to choose the
tunnel slot and child identity. Runtime state is volatile and disappears when
the system service restarts.

For a stable production hostname and Cloudflare Access policy, use a named
Cloudflare Tunnel instead of a Quick Tunnel.

## Service operations

```bash
systemctl status cloudflared-quick-tunnel.service
journalctl -u cloudflared-quick-tunnel.service
systemctl --user status cloudflared-quick-tunnel-indicator.service
```

Uninstall:

```bash
sudo ./uninstall.sh
```

## License

MIT
