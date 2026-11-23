#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this uninstaller as root (for example: sudo ./uninstall.sh)" >&2
    exit 1
fi

systemctl stop cloudflared-quick-tunnel-update.service 2>/dev/null || true
systemctl disable --now cloudflared-quick-tunnel-auto-update.timer 2>/dev/null || true
systemctl disable --now cloudflared-quick-tunnel.service 2>/dev/null || true
systemctl --global disable cloudflared-quick-tunnel-indicator.service 2>/dev/null || true

rm -f \
    /usr/local/libexec/cloudflared-quick-tunnel-daemon \
    /usr/local/libexec/cloudflared_quick_tunnel_gateway.py \
    /usr/local/libexec/cloudflared_quick_tunnel_terminal.py \
    /usr/local/libexec/cloudflared_quick_tunnel_qtty_holder.py \
    /usr/local/bin/qtty \
    /usr/local/libexec/cloudflared-quick-tunnel-update \
    /usr/local/share/cloudflared-quick-tunnel/revision \
    /usr/local/bin/cloudflared-quick-tunnelctl \
    /usr/local/bin/cloudflared-quick-tunnel-indicator \
    /etc/systemd/system/cloudflared-quick-tunnel.service \
    /etc/systemd/system/cloudflared-quick-tunnel-update.service \
    /etc/systemd/system/cloudflared-quick-tunnel-auto-update.service \
    /etc/systemd/system/cloudflared-quick-tunnel-auto-update.timer \
    /etc/systemd/user/cloudflared-quick-tunnel-indicator.service \
    /usr/share/applications/cloudflared-quick-tunnel-indicator.desktop

systemctl daemon-reload
systemctl reset-failed
echo "Removed Cloudflare Quick Tunnel. Running user indicators exit at logout."
