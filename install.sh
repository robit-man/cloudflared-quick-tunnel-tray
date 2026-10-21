#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root (for example: sudo ./install.sh)" >&2
    exit 1
fi

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

for executable in cloudflared python3 setpriv systemctl; do
    if ! command -v "$executable" >/dev/null 2>&1; then
        echo "Missing required executable: $executable" >&2
        exit 1
    fi
done

if ! python3 - <<'PY'
import gi
for namespace, version in (
    ("Gtk", "3.0"),
    ("Gdk", "3.0"),
    ("AyatanaAppIndicator3", "0.1"),
    ("Notify", "0.7"),
):
    gi.require_version(namespace, version)
    __import__("gi.repository", fromlist=[namespace])
PY
then
    echo "Missing GTK/AppIndicator Python bindings." >&2
    echo "On Ubuntu: apt install python3-gi gir1.2-ayatanaappindicator3-0.1 gir1.2-notify-0.7" >&2
    exit 1
fi

python3 -m py_compile \
    "$project_dir/src/cloudflared_quick_tunnel_daemon.py" \
    "$project_dir/src/cloudflared_quick_tunnel_ctl.py" \
    "$project_dir/src/cloudflared_quick_tunnel_indicator.py"

install -D -o root -g root -m 0755 \
    "$project_dir/src/cloudflared_quick_tunnel_daemon.py" \
    /usr/local/libexec/cloudflared-quick-tunnel-daemon
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
systemctl enable --now cloudflared-quick-tunnel.service
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
            systemctl --user start cloudflared-quick-tunnel-indicator.service
    fi
fi

echo "Installed Cloudflare Quick Tunnel broker and per-user tray indicator."
