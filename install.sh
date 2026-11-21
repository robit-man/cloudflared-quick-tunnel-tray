#!/bin/sh
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "Run this installer as root (for example: sudo ./install.sh)" >&2
    exit 1
fi

project_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)

for executable in cloudflared /usr/bin/python3 /usr/bin/ssh setpriv systemctl flock runuser getent; do
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

for component in ("daemon", "gateway", "ctl", "indicator", "update"):
    path = Path(sys.argv[1], "src", f"cloudflared_quick_tunnel_{component}.py")
    compile(path.read_bytes(), str(path), "exec")
PY


broker=cloudflared-quick-tunnel.service
tray=cloudflared-quick-tunnel-indicator.service
exec 9>/run/cloudflared-quick-tunnel-install.lock
flock 9

user_command() {
    command_uid=$1
    shift
    command_user=$(getent passwd "$command_uid" | cut -d: -f1)
    runuser -u "$command_user" -- env \
        XDG_RUNTIME_DIR="/run/user/$command_uid" \
        DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/$command_uid/bus" "$@"
}

user_ctl() {
    ctl_uid=$1
    shift
    user_command "$ctl_uid" systemctl --user "$@"
}

retire_direct_tray() {
    user_command "$1" /usr/bin/python3 -I - <<'PY'
import os
import select
import signal
import sys
from pathlib import Path

executable = b"/usr/local/bin/cloudflared-quick-tunnel-indicator"

def is_indicator(process):
    try:
        argv = (process / "cmdline").read_bytes().rstrip(b"\0").split(b"\0")
    except (FileNotFoundError, ProcessLookupError, PermissionError):
        return False
    return argv == [executable] or (
        len(argv) == 2 and argv[1] == executable
        and argv[0].rsplit(b"/", 1)[-1].startswith(b"python3")
    )

for process in Path("/proc").iterdir():
    if not process.name.isdecimal() or not is_indicator(process):
        continue
    try:
        if process.stat().st_uid != os.getuid():
            continue
        fd = os.pidfd_open(int(process.name))
    except (FileNotFoundError, ProcessLookupError):
        continue
    try:
        if not is_indicator(process):
            continue
        try:
            signal.pidfd_send_signal(fd, signal.SIGTERM)
        except ProcessLookupError:
            continue
        if not select.select([fd], [], [], 10)[0]:
            sys.exit("The existing tray indicator did not exit")
        print("retired")
    finally:
        os.close(fd)
PY
}

stage=$(mktemp -d /run/cloudflared-quick-tunnel-install.XXXXXX)
manifest=$stage/files
managers=
active_trays=
restart_trays=
broker_was_active=false
broker_enabled_here=false
tray_enabled_here=false
retiring=false
publishing=false

cleanup() {
    result=$?
    trap - EXIT
    if [ "$result" -ne 0 ] && [ "$retiring" = true ]; then
        echo "Installation failed. Restoring the previous service files and startup state." >&2
        if [ "$publishing" = true ]; then
            for uid in $restart_trays; do
                user_ctl "$uid" stop "$tray" || :
            done
            systemctl stop "$broker" || :
            if [ "$broker_enabled_here" = true ]; then
                systemctl disable "$broker" || :
            fi
            if [ "$tray_enabled_here" = true ]; then
                systemctl --global disable "$tray" || :
            fi
            while read -r mode source destination; do
                if [ -e "$stage/old$destination" ] || [ -L "$stage/old$destination" ]; then
                    cp -a --remove-destination -- "$stage/old$destination" "$destination" || :
                else
                    rm -f -- "$destination" || :
                fi
            done < "$manifest"
            systemctl daemon-reload || :
            for uid in $managers; do
                user_ctl "$uid" daemon-reload || :
            done
        fi
        if [ "$broker_was_active" = true ]; then
            systemctl start "$broker" || :
        fi
        for uid in $active_trays; do
            user_ctl "$uid" start "$tray" || :
        done
    fi
    rm -rf -- "$stage"
    exit "$result"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM

revision=
if [ -f "$project_dir/REVISION" ]; then
    revision=$(cat "$project_dir/REVISION")
elif [ -e "$project_dir/.git" ] && command -v git >/dev/null 2>&1; then
    revision=$(git -C "$project_dir" rev-parse --verify HEAD 2>/dev/null || :)
fi
case "$revision" in
    *[!0-9a-f]*) revision= ;;
esac
if [ "${#revision}" -ne 40 ]; then
    revision=unknown
fi
printf '%s\n' "$revision" > "$stage/revision"

cat > "$manifest" <<'FILES'
0755 src/cloudflared_quick_tunnel_daemon.py /usr/local/libexec/cloudflared-quick-tunnel-daemon
0644 src/cloudflared_quick_tunnel_gateway.py /usr/local/libexec/cloudflared_quick_tunnel_gateway.py
0755 src/cloudflared_quick_tunnel_update.py /usr/local/libexec/cloudflared-quick-tunnel-update
0644 revision /usr/local/share/cloudflared-quick-tunnel/revision
0755 src/cloudflared_quick_tunnel_ctl.py /usr/local/bin/cloudflared-quick-tunnelctl
0755 src/cloudflared_quick_tunnel_indicator.py /usr/local/bin/cloudflared-quick-tunnel-indicator
0644 systemd/cloudflared-quick-tunnel.service /etc/systemd/system/cloudflared-quick-tunnel.service
0644 systemd/cloudflared-quick-tunnel-update.service /etc/systemd/system/cloudflared-quick-tunnel-update.service
0644 systemd/cloudflared-quick-tunnel-indicator.service /etc/systemd/user/cloudflared-quick-tunnel-indicator.service
0644 xdg/cloudflared-quick-tunnel-indicator.desktop /usr/share/applications/cloudflared-quick-tunnel-indicator.desktop
FILES

while read -r mode source destination; do
    source_path=$project_dir/$source
    if [ "$source" = revision ]; then
        source_path=$stage/revision
    fi
    install -D -o root -g root -m "$mode" "$source_path" "$stage/new$destination"
    if [ -e "$destination" ] || [ -L "$destination" ]; then
        mkdir -p "$(dirname "$stage/old$destination")"
        cp -a -- "$destination" "$stage/old$destination"
    fi
done < "$manifest"

desktop_user=$(printenv SUDO_USER || :)
desktop_uid=
if [ -n "$desktop_user" ] && [ "$desktop_user" != root ]; then
    desktop_uid=$(id -u "$desktop_user")
fi

for bus in /run/user/[0-9]*/bus; do
    [ -S "$bus" ] || continue
    uid=$(basename "$(dirname "$bus")")
    [ "$uid" -ne 0 ] || continue
    state=$(user_ctl "$uid" show --property=ActiveState --value "$tray") || continue
    managers="$managers $uid"
    case "$state" in
        active|activating|reloading)
            active_trays="$active_trays $uid"
            ;;
    esac
done

if systemctl is-active --quiet "$broker"; then
    broker_was_active=true
fi

retiring=true
for uid in $managers; do
    state=$(user_ctl "$uid" show --property=LoadState --value "$tray")
    if [ "$state" != not-found ]; then
        user_ctl "$uid" stop "$tray"
    fi
    # Older desktop launchers could run the tray directly outside its service.
    retired=$(retire_direct_tray "$uid")
    case " $active_trays " in
        *" $uid "*) restart_trays="$restart_trays $uid" ;;
        *)
            if [ -n "$retired" ]; then
                active_trays="$active_trays $uid"
                restart_trays="$restart_trays $uid"
            elif [ "$uid" = "$desktop_uid" ]; then
                restart_trays="$restart_trays $uid"
            fi
            ;;
    esac
done
if [ "$(systemctl show --property=LoadState --value "$broker")" != not-found ]; then
    systemctl stop "$broker"
fi

publishing=true
while read -r mode source destination; do
    install -D -o root -g root -m "$mode" "$stage/new$destination" "$destination"
done < "$manifest"

systemctl daemon-reload
systemctl start "$broker"
for _attempt in 1 2 3 4 5 6 7 8 9 10; do
    if /usr/local/bin/cloudflared-quick-tunnelctl ping >/dev/null 2>&1; then
        break
    fi
    sleep .2
done
/usr/local/bin/cloudflared-quick-tunnelctl ping >/dev/null

if ! systemctl is-enabled --quiet "$broker"; then
    broker_enabled_here=true
fi
if ! systemctl --global is-enabled --quiet "$tray"; then
    tray_enabled_here=true
fi
systemctl enable "$broker"
systemctl --global enable "$tray"
for uid in $managers; do
    user_ctl "$uid" daemon-reload
done
for uid in $restart_trays; do
    user_ctl "$uid" start "$tray"
done

echo "Installed Cloudflare Quick Tunnel broker and per-user tray indicator."
