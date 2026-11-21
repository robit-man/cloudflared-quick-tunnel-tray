#!/bin/sh
set -eu

fail() {
    printf '%s\n' "$*" >&2
    exit 1
}

as_root() {
    if [ "$is_root" = true ]; then
        "$@"
    else
        sudo -- "$@"
    fi
}

download() {
    url=$1
    destination=$2
    shift 2
    curl -q --proto '=https' --proto-redir '=https' --retry 3 --connect-timeout 15 \
        --max-time 90 --fail --silent --show-error --location --output "$destination" "$@" "$url"
}

install_dependencies() {
    if command -v apt-get >/dev/null 2>&1; then
        as_root apt-get update
        as_root env DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
            python3 python3-gi gir1.2-gtk-3.0 gir1.2-ayatanaappindicator3-0.1 \
            gir1.2-notify-0.7 openssh-client python3-websockets util-linux wl-clipboard pkexec
    elif command -v dnf >/dev/null 2>&1; then
        as_root dnf install -y \
            python3 python3-gobject python3-websockets gtk3 libayatana-appindicator-gtk3 \
            libnotify openssh-clients util-linux wl-clipboard polkit
    elif command -v pacman >/dev/null 2>&1; then
        as_root pacman -S --needed --noconfirm \
            python python-gobject python-websockets gtk3 libayatana-appindicator \
            libnotify openssh util-linux wl-clipboard polkit
    else
        echo "Checking existing dependencies on this distribution."
    fi
}

install_cloudflared() {
    # Match the broker's executable search path.
    if PATH=/usr/local/bin:/usr/bin:/bin command -v cloudflared >/dev/null 2>&1; then
        return
    fi

    machine=$(uname -m)
    case "$machine" in
        x86_64) arch=amd64 ;;
        aarch64) arch=arm64 ;;
        i?86) arch=386 ;;
        armv7*) arch=armhf ;;
        armv6*) arch=arm ;;
        *) fail "No cloudflared download is configured for $machine. Install cloudflared and rerun setup." ;;
    esac

    echo "Downloading cloudflared for $arch."
    download "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$arch" \
        "$work_dir/cloudflared"
    as_root install -D -m 0755 "$work_dir/cloudflared" /usr/local/bin/cloudflared
}

main() {
    if [ ! -d /run/systemd/system ] || ! command -v systemctl >/dev/null 2>&1; then
        fail "Cloudflare Quick Tunnel Tray needs a running systemd system manager."
    fi
    is_root=false
    if [ "$(id -u)" -eq 0 ]; then
        is_root=true
    elif ! command -v sudo >/dev/null 2>&1; then
        fail "Install sudo or run setup as root."
    fi

    work_dir=$(mktemp -d)
    trap 'rm -rf -- "$work_dir"' EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    project_dir=
    if [ "${0##*/}" = setup.sh ]; then
        project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
    fi
    if [ -z "$project_dir" ] || [ ! -f "$project_dir/install.sh" ] || [ ! -d "$project_dir/src" ]; then
        echo "Downloading Cloudflare Quick Tunnel Tray."
        download 'https://api.github.com/repos/robit-man/cloudflared-quick-tunnel-tray/commits/main' \
            "$work_dir/revision" --header 'Accept: application/vnd.github.sha' --header 'Cache-Control: no-cache'
        revision=$(cat "$work_dir/revision")
        case "$revision" in
            *[!0-9a-f]*) fail "GitHub returned an invalid commit ID." ;;
        esac
        [ "${#revision}" -eq 40 ] || fail "GitHub returned an invalid commit ID."
        download "https://codeload.github.com/robit-man/cloudflared-quick-tunnel-tray/tar.gz/$revision" \
            "$work_dir/source.tar.gz"
        project_dir="$work_dir/source"
        mkdir "$project_dir"
        tar -xzf "$work_dir/source.tar.gz" -C "$project_dir" --strip-components=1
        printf '%s\n' "$revision" > "$project_dir/REVISION"
    fi
    [ -f "$project_dir/install.sh" ] || fail "The project archive is missing install.sh."

    install_dependencies
    install_cloudflared
    as_root sh "$project_dir/install.sh"
}

main
