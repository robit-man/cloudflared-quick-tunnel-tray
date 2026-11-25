#!/usr/bin/env bash
set -euo pipefail

version=$1
arch=$2
cloudflared=$3
output=$4
stage=$(mktemp -d)
trap 'rm -rf "$stage"' EXIT
mkdir -p "$stage/DEBIAN" "$output"

# Use the source installer's file list. Apt owns updates to this installation.
while read -r mode source destination; do
    case "$source" in
        *update*|revision) continue ;;
    esac
    case "$destination" in
        /usr/local/*) destination=/usr/${destination#/usr/local/} ;;
        /etc/systemd/*) destination=/usr/lib/systemd/${destination#/etc/systemd/} ;;
    esac
    mkdir -p "$stage$(dirname "$destination")"
    case "$source" in
        systemd/*) sed 's@/usr/local/@/usr/@g' "$source" > "$stage$destination" ;;
        *) cp "$source" "$stage$destination" ;;
    esac
    chmod "$mode" "$stage$destination"
done < <(awk '/^cat > "\$manifest" <<.FILES./ { files=1; next } /^FILES$/ { files=0 } files' install.sh)

install -Dm755 "$cloudflared" "$stage/usr/libexec/cloudflared-quick-tunnel-cloudflared"
install -Dm644 "$(dirname "$cloudflared")/cloudflared-LICENSE" "$stage/usr/share/doc/cloudflared-quick-tunnel-tray/cloudflared-LICENSE"
install -Dm644 LICENSE "$stage/usr/share/doc/cloudflared-quick-tunnel-tray/copyright"
install -Dm644 README.md "$stage/usr/share/doc/cloudflared-quick-tunnel-tray/README.md"
cat > "$stage/DEBIAN/control" <<CONTROL
Package: cloudflared-quick-tunnel-tray
Version: $version
Section: net
Priority: optional
Architecture: $arch
Maintainer: robit-man <robit-man@users.noreply.github.com>
Depends: init-system-helpers, systemd, util-linux, python3 (>= 3.10), python3-gi, python3-cryptography, python3-websockets, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1, gir1.2-notify-0.7, openssh-client
Recommends: wl-clipboard
Suggests: openssh-server
Description: Cloudflare Quick Tunnel tray and encrypted browser terminal gateway
 Creates per-user SSH and TCP Quick Tunnels and provides a browser terminal
 protected by a Noise-encrypted channel to the local host.
CONTROL
for script in preinst postinst prerm postrm; do
    install -m 0755 "packaging/debian-$script" "$stage/DEBIAN/$script"
done

dpkg-deb --root-owner-group --build "$stage" "$output/quick-tunnel-tray-linux-$arch.deb"
