# Cloudflared Quick Tunnel Tray

## Install

Choose **Install** in [TTY](https://robit-man.github.io/tty/) or download a package from the [latest release](https://github.com/robit-man/cloudflared-quick-tunnel-tray/releases/latest).

- **macOS (Apple silicon or Intel)** — Open the matching `.dmg`, drag the app to Applications, and launch it. Enable **Remote Login** in System Settings → General → Sharing for Browser Terminal and select which accounts may log in.
- **Windows (x64)** — Run the `.exe` installer and open the tray from Start. Browser Terminal uses a non-root user and OpenSSH in the default WSL 2 distribution. On Debian or Ubuntu in WSL, run `sudo apt install openssh-server python3-cryptography python3-websockets`, then `sudo service ssh start`. Use your WSL SSH username in TTY. The native SSH tunnel action targets the Windows OpenSSH server.
- **Debian / Ubuntu (x64 or ARM64)** — Run `sudo apt install ./quick-tunnel-tray-linux-<arch>.deb` and open Cloudflare Quick Tunnel from your applications menu. Enable your local SSH server for Browser Terminal. Update this installation by installing the newer package with apt. For older `setup.sh` installations, keep using `setup.sh` or remove the source installation before switching to apt.

macOS builds are ad hoc signed and Windows installers are unsigned. The release publishes SHA-256 checksums for all five downloads.

## Linux setup from source

```sh
curl -fsSL https://raw.githubusercontent.com/robit-man/cloudflared-quick-tunnel-tray/main/setup.sh | sh
```

Run this from your desktop session. Setup installs dependencies on Debian,
Ubuntu, Fedora, and Arch Linux, downloads `cloudflared` if needed, and starts
the broker and your tray indicator. It uses `sudo` for system installation.

[![CI](https://github.com/robit-man/cloudflared-quick-tunnel-tray/actions/workflows/ci.yml/badge.svg)](https://github.com/robit-man/cloudflared-quick-tunnel-tray/actions/workflows/ci.yml)

A tray app for creating disposable
[Cloudflare Quick Tunnels](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/).

The default tray action publishes the host's SSH service on port 22. You can
also select any listening local TCP port, cycle to a new random
`trycloudflare.com` endpoint, stop the tunnel, or copy the endpoint hostname.
Browser Terminal mode runs a WebSocket gateway for the same local SSH service.
The browser uses a standalone static page and needs no `cloudflared` installation.

## What Linux installs

- A hardened system service that brokers Quick Tunnels.
- One isolated tunnel slot per Unix UID, authenticated with `SO_PEERCRED`.
- A globally enabled systemd user service that starts the tray in graphical
  sessions and stops that user's tunnel when the tray session ends.
- A CLI for scripting and troubleshooting.
- `qtty`, which keeps terminal sessions running between browser visits.

The broker drops to the requesting Unix account before starting tunnel
processes. Browser Terminal mode requires a non-root requester and starts its
gateway on a private IPv4 loopback port. SSH authentication uses the host's
existing SSH daemon on `127.0.0.1:22`.

## Linux requirements

- Linux with systemd and a desktop panel that supports AppIndicator
- Python 3.10 or newer
- `cloudflared`
- `setpriv` from util-linux
- GTK 3, PyGObject, Ayatana AppIndicator, and libnotify bindings
- OpenSSH client and the system Python `websockets` and `cryptography` packages
- A running SSH daemon on `127.0.0.1:22` for Browser Terminal mode

On other distributions, install the system dependencies before running setup.

On Wayland, the copy actions use `wl-copy` from the `wl-clipboard` package
when available and falls back to the desktop's XWayland clipboard bridge.

## Install from a checkout

```bash
git clone https://github.com/robit-man/cloudflared-quick-tunnel-tray.git
cd cloudflared-quick-tunnel-tray
./setup.sh
```

The installer starts the system broker immediately and enables the tray for
all users' future graphical sessions. Run setup from your desktop session to
start your tray immediately. The installer stops existing tray instances and
the broker before replacing their files, then starts the updated services.
Upgrading ends active tunnels and browser sessions.

## Update a Linux setup.sh installation

The tray checks this repository’s GitHub `main` branch at startup and every 30
minutes. It shows an update icon when the GitHub commit differs from the
installed revision. Open the tray and select **Install update**. The desktop
may request administrator authorization through Polkit. The system update
service downloads the exact GitHub commit, runs the installer, and restarts
the broker and tray.

Select **Check for updates** for an immediate check. Installations from before
this feature need one upgrade using the setup command above. If your desktop
has no Polkit authentication agent, you can run
`sudo systemctl start cloudflared-quick-tunnel-update.service` after that
upgrade. See update errors with
`sudo journalctl -u cloudflared-quick-tunnel-update.service`.

### Automatic updates

A system timer checks GitHub 3 minutes after boot and every 5 minutes after
that. When `main` has a new commit, it installs the update, unless a browser
terminal is attached. Installing restarts the broker, which would end those
terminals, so the timer waits and tries again on the next check. qtty sessions
keep running through an update. Tunnels that were running start again in the
same mode and port as their owner, with a new address that the tray shows.

The timer is enabled on a first install, and later installs keep your choice.
Turn it off or back on with:

```bash
sudo systemctl disable --now cloudflared-quick-tunnel-auto-update.timer
sudo systemctl enable --now cloudflared-quick-tunnel-auto-update.timer
```

See its decisions with
`journalctl -u cloudflared-quick-tunnel-auto-update.service`.

## Tray actions

- **Copy endpoint** — copies only the generated hostname.
- **Start / cycle SSH tunnel (port 22)** — replaces the current endpoint with
  a fresh SSH tunnel.
- **Start / cycle browser terminal** — publishes a new Browser Terminal endpoint.
- **Copy browser terminal link** — copies the browser link with its per-tunnel token.
- **Browser sessions** — shows attached browser terminals for your tunnel.
  Open an entry and choose **End session** to close only that connection.
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
cloudflared-quick-tunnelctl start --browser
cloudflared-quick-tunnelctl cycle --browser
cloudflared-quick-tunnelctl stop
```

Add `--json` before or after the subcommand for structured output.
`status --json` includes attached browser terminals in `browser_sessions`.
Run `cloudflared-quick-tunnelctl end-session SESSION_ID` to close one of them.
Browser `start` and `cycle` print the pasteable link. `status --json` includes
it as `browser_url` while the tunnel is ready. A bare `cycle` keeps the current
mode and port. Use `--port 22` to switch back to native SSH.

To connect to an SSH Quick Tunnel from a machine with `cloudflared` installed:

```bash
ssh -o 'ProxyCommand=cloudflared access ssh --hostname %h' USER@ENDPOINT
```

For a general TCP service, create a local client-side listener first:

```bash
cloudflared access tcp --hostname ENDPOINT --url localhost:9000
```

Then point the application at `localhost:9000`.

## Browser Terminal

1. Open [TTY](https://robit-man.github.io/tty/) in a current browser with Web Crypto X25519 support, such as Safari 18.4 or later. You can also host the standalone [index.html](index.html) on an HTTPS origin you trust.
2. Choose **Start / cycle browser terminal** in the updated tray. On Linux, you can also run `cloudflared-quick-tunnelctl start --browser`.
3. Choose **Copy browser terminal link**. Paste the full `https://…trycloudflare.com/#tty=v2.…` link into TTY, enter your SSH username, and select **Connect**. Cycle the tunnel to replace links from earlier versions.
4. Complete the OpenSSH authentication prompt in the terminal. Use **Disconnect** to end the session, or **Stop tunnel** from the tray to close every attached session. Cycling ends all sessions and issues a fresh link.

On Linux, the tray lists browsers after the encrypted login request. The
displayed SSH username comes from the browser's encrypted login request. The browser
and its PTY both close when you end that entry.

Sessions survive network changes. Moving a phone from Wi-Fi to cellular, or
waking a laptop, drops the browser's WebSocket without a clean close. The
gateway keeps the SSH PTY running for up to 15 minutes and buffers its latest
1 MiB of output. The page sends heartbeats and also checks the connection when
the browser reports a network or visibility change. When a check fails, it
opens a new WebSocket and resumes the same PTY. Missed output is replayed from
the last byte the page received. Keys typed while offline are sent once the
session resumes, and the gateway's input count stops any byte from being typed
twice. The Linux tray shows a detached session as waiting to reconnect.
**Disconnect** and **End session** close the PTY. If the browser closes before
it can deliver the encrypted disconnect, the gateway uses the same timeout.

On touch screens, drag with two fingers to scroll. The page delivers the
gesture as mouse-wheel input, the same as a desktop wheel or trackpad, so
full-screen programs that track the mouse (Claude Code, Codex, `vim`, `less`)
scroll their own views. Other full-screen programs get arrow keys, and a plain
shell scrolls the local scrollback.

### Saved sessions with qtty

Linux browser logins open in `qtty`, so a terminal keeps running after you close
the tab, lose the network for longer than 15 minutes, or cycle the tunnel.
Each pane remembers its session. Press Enter in a disconnected pane, sign in,
and the pane returns to the same session with its screen restored. After a
reload, the page lists the panes you had open, ready to sign in again. A new
pane starts a session if you have none and otherwise opens an arrow-key
picker of running sessions, which also offers a new session and a plain
shell. Inside a session, Ctrl-] returns to the picker.

The same sessions work over normal SSH:

```bash
qtty            # picker
qtty ls         # list (also: qtty --sessions)
qtty new agent  # start "agent" and attach
qtty attach agent
qtty kill agent
```

Sessions run under your account in a holder process that starts on demand. It
keeps the latest 1 MiB of each session's output to redraw the screen when you
attach. Sessions end when their shell exits, or with `qtty kill` or a reboot.

The fork button in a pane header opens a new session beside it, in the same
working directory. The new pane uses the original pane's SSH login, so it
needs no password while that login is still open. Otherwise, SSH asks again.

Select text on a touch screen by pressing and holding, then dragging. Lift
your finger for Copy, Paste and Select all. On a desktop, right-click opens
the same menu, and Shift-drag (Option-drag on macOS) selects text in programs
that capture the mouse.

Password and keyboard-interactive prompts follow the local SSH daemon's
configuration. To use public-key authentication, generate a dedicated OpenSSH
key with a nonempty passphrase as the Unix user who starts the tunnel.

```bash
ssh-keygen -t ed25519 -f ~/.ssh/cloudflared-quick-tunnel
```

Authorize its `.pub` file for the destination SSH account using your normal SSH
administration process. Start or cycle Browser Terminal mode after configuring
the key, then enter the key's passphrase at the prompt. The gateway uses only
this passphrase-protected identity, with SSH agent and default keys disabled.
The local SSH host key is verified using `/etc/ssh/ssh_host_*_key.pub` and the
usual OpenSSH known-hosts files. If sshd uses a custom key path, pin its public
host key for `127.0.0.1` in your `~/.ssh/known_hosts` before connecting.

## Security notes

Quick Tunnels create public Cloudflare endpoints and have no uptime guarantee.
SSH still requires SSH authentication, but a custom TCP service may not have
authentication of its own. Do not publish an unauthenticated administrative,
database, or development port.

Treat the version 2 Browser Terminal link as a secret. Share it through a
trusted channel and paste it into a TTY page you trust. The fragment contains
a random, 32-byte pre-shared key. The browser and gateway use
[Noise NNpsk0](https://noiseprotocol.org/noise.html#handshake-patterns)
with ephemeral X25519, AES-256-GCM, and SHA-256 on every WebSocket, including
resume and fork. The key stays at the endpoints. The handshake must succeed
before the gateway accepts a login, and v2 rejects the old plaintext route.
Cloudflare relays encrypted frames and can see metadata such as the hostname,
timing, and sizes. The gateway decrypts terminal traffic on the host and runs
OpenSSH with local host-key verification. TTY's Recents saves links in that
site's browser storage.

After login, the gateway gives the browser a separate per-session resume
secret over the encrypted connection. The browser keeps it in memory until
the session ends. The gateway still requires SSH authentication. A fork
uses the parent session's secret and reuses its authenticated OpenSSH control
socket while it is available. The control connection closes 60 seconds
after its last terminal.

qtty keeps its socket in `/tmp/qtty-UID`, and refuses to start unless the
directory is mode 700 and owned by you. The holder also checks each client's
peer UID.

The control socket is intentionally reachable by local users. The daemon uses
the kernel-provided peer UID, not a client-supplied username, to choose the
tunnel slot and child identity. Runtime state is volatile and disappears when
the system service restarts.

For a stable production hostname and Cloudflare Access policy, use a named
Cloudflare Tunnel instead of a Quick Tunnel.

## Releases

Push a version tag such as `v2.0.0` to build and publish the two macOS disk
images, Windows installer, two Debian packages, and checksum file. Every
installer includes a version and checksum-pinned Cloudflare binary. Tagged CI
builds the artifacts on their target platforms before publishing the release.

## Service operations

```bash
systemctl status cloudflared-quick-tunnel.service
journalctl -u cloudflared-quick-tunnel.service
systemctl --user status cloudflared-quick-tunnel-indicator.service
```

From a repository checkout, uninstall with:

```bash
sudo ./uninstall.sh
```

## License

MIT
