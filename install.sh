#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root (for example: sudo ./install.sh)" >&2
    exit 1
fi

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

for executable in cloudflared /usr/bin/python3 /usr/bin/ssh setpriv systemctl; do
    if ! command -v "$executable" >/dev/null 2>&1; then
        echo "Missing required executable: $executable" >&2
        exit 1
    fi
done

/usr/bin/python3 -I - "$project_dir" <<'PY'
import sys
from pathlib import Path

if sys.version_info < (3, 10):
    sys.exit("Python 3.10 or newer is required.")

try:
    from websockets.legacy.server import serve
except ImportError as exc:
    sys.exit(f"Could not load the system websockets package ({exc}). Run ./setup.sh to install dependencies.")

try:
    import gi

    for namespace, version in (
        ("Gtk", "3.0"),
        ("Gdk", "3.0"),
        ("AyatanaAppIndicator3", "0.1"),
        ("Notify", "0.7"),
    ):
        gi.require_version(namespace, version)
        __import__("gi.repository", fromlist=[namespace])
except (ImportError, ValueError) as exc:
    sys.exit(f"Could not load GTK or AppIndicator bindings ({exc}). Run ./setup.sh to install dependencies.")

for component in ("daemon", "gateway", "ctl", "indicator"):
    path = Path(sys.argv[1], "src", f"cloudflared_quick_tunnel_{component}.py")
    compile(path.read_bytes(), str(path), "exec")
PY

install -D -o root -g root -m 0755 \
    "$project_dir/src/cloudflared_quick_tunnel_daemon.py" \
    /usr/local/libexec/cloudflared-quick-tunnel-daemon
install -D -o root -g root -m 0644 \
    "$project_dir/src/cloudflared_quick_tunnel_gateway.py" \
    /usr/local/libexec/cloudflared_quick_tunnel_gateway.py
install -D -o root -g root -m 0755 \
    "$project_dir/src/cloudflared_quick_tunnel_ctl.py" \
    /usr/local/bin/cloudflared-quick-tunnelctl
install -D -o root -g root -m 0755 \
    "$project_dir/src/cloudflared_quick_tunnel_indicator.py" \
    /usr/local/bin/cloudflared-quick-tunnel-indicator
install -D -o root -g root -m 0644 \
    "$project_dir/systemd/cloudflared-quick-tunnel.service" \
    /etc/systemd/system/cloudflared-quick-tunnel.service
install -D -o root -g root -m 0644 \
    "$project_dir/systemd/cloudflared-quick-tunnel-indicator.service" \
    /etc/systemd/user/cloudflared-quick-tunnel-indicator.service
install -D -o root -g root -m 0644 \
    "$project_dir/xdg/cloudflared-quick-tunnel-indicator.desktop" \
    /usr/share/applications/cloudflared-quick-tunnel-indicator.desktop

systemctl daemon-reload
systemctl enable cloudflared-quick-tunnel.service
systemctl restart cloudflared-quick-tunnel.service
systemctl --global enable cloudflared-quick-tunnel-indicator.service

desktop_user=${SUDO_USER:-}
if [ -n "$desktop_user" ] && [ "$desktop_user" != root ]; then
    desktop_uid=$(id -u "$desktop_user")
    if [ -S "/run/user/$desktop_uid/bus" ]; then
        runuser -u "$desktop_user" -- env \
            XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
            systemctl --user daemon-reload
        runuser -u "$desktop_user" -- env \
            XDG_RUNTIME_DIR="/run/user/$desktop_uid" \
            DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$desktop_uid/bus" \
            systemctl --user restart cloudflared-quick-tunnel-indicator.service
    fi
fi

echo "Installed Cloudflare Quick Tunnel broker and per-user tray indicator."
