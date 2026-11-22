#!/usr/bin/python3
"""Ayatana tray indicator for per-user Cloudflare Quick Tunnels."""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Notify", "0.7")
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402
from gi.repository import Gdk, GLib, Gtk, Notify  # noqa: E402


SOCKET_PATH = Path("/run/cloudflared-quick-tunnel/control.sock")
APP_ID = "cloudflared-quick-tunnel-indicator"
UPDATE_HELPER = "/usr/local/libexec/cloudflared-quick-tunnel-update"
UPDATE_UNIT = "cloudflared-quick-tunnel-update.service"
SYSTEMCTL = "/usr/bin/systemctl"


def service_request(payload: dict[str, Any], timeout: float = 50.0) -> dict[str, Any]:
    connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    connection.settimeout(timeout)
    try:
        connection.connect(str(SOCKET_PATH))
        connection.sendall(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > 64 * 1024:
                raise RuntimeError("Service response is too large")
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise RuntimeError("Quick Tunnel service is not running") from exc
    except socket.timeout as exc:
        raise RuntimeError("Quick Tunnel service did not respond in time") from exc
    finally:
        connection.close()
    if not data:
        raise RuntimeError("Quick Tunnel service closed without a response")
    try:
        response = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Quick Tunnel service returned invalid data") from exc
    if not response.get("ok"):
        raise RuntimeError(response.get("error", "Request failed"))
    return response


def check_update() -> str | None:
    result = subprocess.run(
        ["/usr/bin/python3", "-I", UPDATE_HELPER, "--check"],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode:
        raise RuntimeError("Couldn’t check GitHub for an update. Try again later.")
    revisions = json.loads(result.stdout)
    return revisions["latest"] if revisions["latest"] != revisions["installed"] else None


def apply_update() -> None:
    # Use noninteractive sudo only when the administrator has already authorized it.
    command = [SYSTEMCTL, "start", "--no-block", UPDATE_UNIT]
    sudo = "/usr/bin/sudo"
    scheduled = os.access(sudo, os.X_OK) and subprocess.run(
        [sudo, "-n", *command], capture_output=True, timeout=10,
    ).returncode == 0
    if not scheduled:
        pkexec = "/usr/bin/pkexec"
        if not os.access(pkexec, os.X_OK):
            raise RuntimeError(f"Administrator authorization is needed. Run sudo systemctl start {UPDATE_UNIT}.")
        authorized = subprocess.run(
            [pkexec, "--disable-internal-agent", *command], capture_output=True, timeout=120,
        ).returncode
        if authorized == 126:
            raise RuntimeError("The update was cancelled.")
        if authorized:
            raise RuntimeError(f"Couldn’t authorize the update. Run sudo systemctl start {UPDATE_UNIT}.")

    # The system manager owns the job and keeps running when the tray is replaced.
    deadline = time.monotonic() + 195
    while time.monotonic() < deadline:
        status = subprocess.run(
            [SYSTEMCTL, "show", "--property=ActiveState,Result,Job", UPDATE_UNIT],
            capture_output=True, text=True, check=True, timeout=5,
        )
        properties = dict(line.split("=", 1) for line in status.stdout.splitlines() if "=" in line)
        active = properties.get("ActiveState") in {"activating", "active", "deactivating"}
        if active or properties.get("Job", "") not in {"", "0"}:
            time.sleep(1)
            continue
        if properties.get("Result") == "success":
            return
        raise RuntimeError(f"The update failed. Details: journalctl -u {UPDATE_UNIT}")
    raise RuntimeError(f"The update did not finish in time. Check systemctl status {UPDATE_UNIT}.")


class TunnelIndicator:
    def __init__(self) -> None:
        Notify.init(APP_ID)
        self.endpoint: str | None = None
        self.browser_url: str | None = None
        self.xwayland_display: Gdk.Display | None = None
        self.copy_pending = False
        self.current_port = 22
        self.busy = False
        self.last_state: str | None = None
        self.browser_sessions: list[dict[str, Any]] | None = None
        self.pending_session_end: str | None = None
        self.tunnel_icon = ("network-offline-symbolic", "Quick Tunnel stopped")
        self.available_revision: str | None = None
        self.checking_update = False
        self.installing_update = False

        self.indicator = AppIndicator.Indicator.new(
            APP_ID,
            "network-offline-symbolic",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self.indicator.set_title("Cloudflare Quick Tunnel")
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)

        self.menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label="Checking tunnel status…")
        self.status_item.set_sensitive(False)
        self.menu.append(self.status_item)
        self.menu.append(Gtk.SeparatorMenuItem())

        self.copy_item = Gtk.MenuItem(label="Copy endpoint")
        self.copy_item.connect("activate", self.copy_endpoint)
        self.copy_item.set_sensitive(False)
        self.menu.append(self.copy_item)

        self.copy_browser_item = Gtk.MenuItem(label="Copy browser terminal link")
        self.copy_browser_item.connect("activate", self.copy_browser_link)
        self.copy_browser_item.set_sensitive(False)
        self.menu.append(self.copy_browser_item)

        self.sessions_item = Gtk.MenuItem(label="Browser sessions (0)")
        self.sessions_item.set_no_show_all(True)
        self.menu.append(self.sessions_item)

        self.ssh_item = Gtk.MenuItem(label="Start / cycle SSH tunnel (port 22)")
        self.ssh_item.connect("activate", self.start_ssh)
        self.menu.append(self.ssh_item)

        self.browser_item = Gtk.MenuItem(label="Start / cycle browser terminal")
        self.browser_item.connect("activate", self.start_browser)
        self.menu.append(self.browser_item)

        self.custom_item = Gtk.MenuItem(label="Forward custom TCP port…")
        self.custom_item.connect("activate", self.choose_custom_port)
        self.menu.append(self.custom_item)

        self.stop_item = Gtk.MenuItem(label="Stop tunnel")
        self.stop_item.connect("activate", self.stop_tunnel)
        self.stop_item.set_sensitive(False)
        self.menu.append(self.stop_item)

        self.refresh_item = Gtk.MenuItem(label="Refresh status")
        self.refresh_item.connect("activate", lambda _item: self.refresh())
        self.menu.append(self.refresh_item)

        self.menu.append(Gtk.SeparatorMenuItem())
        self.update_item = Gtk.MenuItem(label="Check for updates")
        self.update_item.connect("activate", self.select_update)
        self.menu.append(self.update_item)

        self.quit_item = Gtk.MenuItem(label="Quit indicator")
        self.quit_item.connect("activate", lambda _item: Gtk.main_quit())
        self.menu.append(self.quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.refresh()
        self.check_for_updates()
        GLib.timeout_add_seconds(4, self.periodic_refresh)
        GLib.timeout_add_seconds(30 * 60, self.check_for_updates)

    def periodic_refresh(self) -> bool:
        if not self.busy:
            self.refresh()
        return True

    def notify(self, title: str, body: str, icon: str = "network-vpn-symbolic") -> None:
        try:
            notification = Notify.Notification.new(title, body, icon)
            notification.set_timeout(5000)
            notification.show()
        except GLib.Error:
            pass

    def set_tunnel_icon(self, icon: str, tooltip: str) -> None:
        self.tunnel_icon = (icon, tooltip)
        self.refresh_icon()

    def refresh_icon(self) -> None:
        if self.installing_update:
            self.indicator.set_icon_full("system-software-install-symbolic", "Installing Quick Tunnel update")
        elif self.available_revision:
            self.indicator.set_icon_full("software-update-available-symbolic", "Quick Tunnel update available")
        else:
            self.indicator.set_icon_full(*self.tunnel_icon)

    def update_menu_label(self) -> None:
        if self.installing_update:
            label = "Installing update…"
        elif self.checking_update:
            label = "Checking for updates…"
        elif self.available_revision:
            label = f"Install update ({self.available_revision[:8]})…"
        else:
            label = "Check for updates"
        self.update_item.set_label(label)
        self.update_item.set_sensitive(not (self.checking_update or self.installing_update))
        self.refresh_icon()

    def check_for_updates(self, manual: bool = False) -> bool:
        if self.checking_update or self.installing_update:
            return True
        self.checking_update = True
        self.update_menu_label()

        def worker() -> None:
            try:
                revision = check_update()
                GLib.idle_add(self.update_checked, revision, None, manual)
            except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as exc:
                GLib.idle_add(self.update_checked, None, str(exc), manual)

        threading.Thread(target=worker, name="check-update", daemon=True).start()
        return True

    def update_checked(self, revision: str | None, error: str | None, manual: bool) -> bool:
        self.checking_update = False
        if not error:
            self.available_revision = revision
        self.update_menu_label()
        if manual:
            if error:
                self.notify("Update check failed", error, "dialog-error-symbolic")
            elif not revision:
                self.notify("Quick Tunnel is up to date", "You have the current version.")
        return False

    def select_update(self, _item: Gtk.MenuItem) -> None:
        if self.installing_update or self.checking_update:
            return
        if not self.available_revision:
            self.check_for_updates(manual=True)
            return
        self.installing_update = True
        self.update_menu_label()

        def worker() -> None:
            try:
                apply_update()
                GLib.idle_add(self.update_finished, None)
            except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
                GLib.idle_add(self.update_finished, str(exc))

        threading.Thread(target=worker, name="install-update", daemon=True).start()

    def update_finished(self, error: str | None) -> bool:
        self.installing_update = False
        if not error:
            self.available_revision = None
        self.update_menu_label()
        if error:
            self.notify("Quick Tunnel update", error, "dialog-error-symbolic")
        return False

    def set_busy(self, message: str) -> None:
        self.busy = True
        self.status_item.set_label(message)
        self.set_tunnel_icon("network-transmit-receive-symbolic", message)
        self.ssh_item.set_sensitive(False)
        self.browser_item.set_sensitive(False)
        self.custom_item.set_sensitive(False)
        self.stop_item.set_sensitive(False)
        self.refresh_item.set_sensitive(False)
        self.copy_item.set_sensitive(False)
        self.copy_browser_item.set_sensitive(False)
        self.sessions_item.set_sensitive(False)

    def run_request(
        self,
        payload: dict[str, Any],
        busy_message: str | None = None,
        callback: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if self.busy:
            return
        if busy_message:
            self.set_busy(busy_message)
        else:
            self.busy = True

        def worker() -> None:
            try:
                response = service_request(payload)
                GLib.idle_add(self.request_complete, response, None, callback)
            except Exception as exc:  # UI boundary: report concise service error
                GLib.idle_add(self.request_complete, None, str(exc), callback)

        threading.Thread(target=worker, name="tunnel-request", daemon=True).start()

    def request_complete(
        self,
        response: dict[str, Any] | None,
        error: str | None,
        callback: Callable[[dict[str, Any]], None] | None,
    ) -> bool:
        self.busy = False
        if error:
            self.pending_session_end = None
            self.apply_error(error)
            return False
        assert response is not None
        self.apply_status(response)
        if callback:
            callback(response)
        if self.pending_session_end is not None:
            session_id = self.pending_session_end
            self.pending_session_end = None
            self.end_browser_session(None, session_id)
        return False

    def refresh(self) -> None:
        self.run_request({"action": "status"})

    def start_ssh(self, _item: Gtk.MenuItem) -> None:
        self.run_request(
            {"action": "cycle", "port": 22},
            "Creating a new SSH tunnel…",
            self.tunnel_started,
        )

    def start_browser(self, _item: Gtk.MenuItem) -> None:
        self.run_request(
            {"action": "cycle", "mode": "browser-tty"},
            "Creating a new browser terminal tunnel…",
            self.tunnel_started,
        )

    def update_browser_sessions(self, sessions: list[dict[str, Any]], visible: bool) -> None:
        self.sessions_item.set_visible(visible)
        self.sessions_item.set_sensitive(visible)
        if sessions == self.browser_sessions:
            return
        self.browser_sessions = sessions
        self.sessions_item.set_label(f"Browser sessions ({len(sessions)})")
        menu = Gtk.Menu()
        if not sessions:
            empty = Gtk.MenuItem(label="No active browser sessions")
            empty.set_sensitive(False)
            menu.append(empty)
        for session in sessions:
            session_id = session["id"]
            connected = datetime.fromisoformat(session["connected_at"]).astimezone().strftime("%H:%M:%S")
            entry = Gtk.MenuItem(label=f"{session['user']} · {session_id[:8]}")
            details = Gtk.Menu()
            if session.get("attached", True):
                labels = [f"Browser attached at {connected}"]
            else:
                labels = [f"Waiting for browser to reconnect (last attached {connected})"]
            if session.get("client_ip"):
                labels.append(f"Client IP {session['client_ip']}")
            for label in labels:
                item = Gtk.MenuItem(label=label)
                item.set_sensitive(False)
                details.append(item)
            end = Gtk.MenuItem(label="End session")
            end.connect("activate", self.end_browser_session, session_id)
            details.append(end)
            entry.set_submenu(details)
            menu.append(entry)
        menu.show_all()
        self.sessions_item.set_submenu(menu)

    def end_browser_session(self, _item: Gtk.MenuItem | None, session_id: str) -> None:
        if self.busy:
            self.pending_session_end = session_id
            return
        self.run_request({"action": "end-session", "id": session_id}, "Ending browser session…")

    def choose_custom_port(self, _item: Gtk.MenuItem) -> None:
        dialog = Gtk.Dialog(
            title="Forward a local TCP port",
            flags=Gtk.DialogFlags.MODAL,
        )
        dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
        dialog.add_button("Start / cycle", Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)
        dialog.set_resizable(False)

        box = dialog.get_content_area()
        box.set_spacing(10)
        box.set_border_width(14)
        explanation = Gtk.Label(
            label=(
                "Choose a local TCP port. This publishes that service through "
                "Cloudflare; make sure the service has its own authentication."
            )
        )
        explanation.set_line_wrap(True)
        explanation.set_max_width_chars(52)
        explanation.set_xalign(0)
        box.add(explanation)

        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        label = Gtk.Label(label="Local port:")
        label.set_xalign(0)
        adjustment = Gtk.Adjustment(
            value=float(self.current_port or 22),
            lower=1,
            upper=65535,
            step_increment=1,
            page_increment=100,
        )
        spinner = Gtk.SpinButton(adjustment=adjustment, climb_rate=1, digits=0)
        spinner.set_numeric(True)
        spinner.set_activates_default(True)
        row.pack_start(label, False, False, 0)
        row.pack_start(spinner, True, True, 0)
        box.add(row)
        dialog.show_all()
        response = dialog.run()
        port = spinner.get_value_as_int()
        dialog.destroy()
        if response == Gtk.ResponseType.OK:
            self.run_request(
                {"action": "cycle", "port": port},
                f"Creating a tunnel to localhost:{port}…",
                self.tunnel_started,
            )

    def stop_tunnel(self, _item: Gtk.MenuItem) -> None:
        self.run_request(
            {"action": "stop"},
            "Stopping tunnel…",
            lambda _response: self.notify(
                "Quick Tunnel stopped", "Your per-user tunnel is no longer available.", "network-offline-symbolic"
            ),
        )

    def tunnel_started(self, response: dict[str, Any]) -> None:
        endpoint = response.get("endpoint")
        port = response.get("port")
        if response.get("browser_url"):
            self.notify("Browser terminal ready", "Use Copy browser terminal link from the tray menu.")
        elif endpoint:
            self.notify(
                "Quick Tunnel ready",
                f"{endpoint} forwards to localhost:{port}. Use Copy endpoint from the tray menu.",
            )

    def copy_endpoint(self, _item: Gtk.MenuItem) -> None:
        if self.endpoint:
            self.copy_text(self.endpoint, "Endpoint copied", self.endpoint)

    def copy_browser_link(self, _item: Gtk.MenuItem) -> None:
        if self.browser_url:
            self.copy_text(self.browser_url, "Browser terminal link copied", "Paste the link into the browser terminal page.")

    def copy_text(self, text: str, title: str, message: str) -> None:
        if self.copy_pending:
            return
        display = Gdk.Display.get_default()
        if display.__gtype__.name == "GdkWaylandDisplay":
            # The tray's D-Bus activation does not give GTK Wayland keyboard focus.
            self.copy_with_wayland(text, title, message)
            return
        self.copy_with_gtk(display, text, title, message)

    def copy_with_gtk(self, display: Gdk.Display, text: str, title: str, message: str) -> None:
        clipboard = Gtk.Clipboard.get_for_display(display, Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(text, -1)
        clipboard.store()
        self.notify(title, message, "edit-copy-symbolic")

    def copy_with_wayland(self, text: str, title: str, message: str) -> None:
        self.copy_pending = True

        def complete(error: str | None) -> bool:
            self.copy_pending = False
            if error is None:
                self.notify(title, message, "edit-copy-symbolic")
                return False

            # Some compositors, including GNOME, bridge the XWayland clipboard.
            x11_display_name = os.environ.get("DISPLAY")
            if x11_display_name and self.xwayland_display is None:
                self.xwayland_display = Gdk.Display.open(x11_display_name)
            if self.xwayland_display is None:
                self.notify("Could not copy to clipboard", error, "dialog-error-symbolic")
            else:
                self.copy_with_gtk(self.xwayland_display, text, title, message)
            return False

        def worker() -> None:
            error = None
            try:
                subprocess.run(
                    ["wl-copy", "--type", "text/plain"],
                    input=text.encode("utf-8"),
                    check=True,
                    timeout=5,
                )
            except FileNotFoundError:
                error = "Install wl-clipboard to copy links in this Wayland session."
            except (OSError, subprocess.SubprocessError):
                error = "The Wayland clipboard did not accept the text."
            GLib.idle_add(complete, error)

        threading.Thread(target=worker, name="copy-endpoint", daemon=True).start()

    def apply_status(self, response: dict[str, Any]) -> None:
        state = response.get("state", "unknown")
        endpoint = response.get("endpoint")
        port = response.get("port")
        browser_mode = response.get("mode") == "browser-tty"
        if isinstance(port, int):
            self.current_port = port
        self.endpoint = endpoint
        self.browser_url = response.get("browser_url")
        self.last_state = state

        ready = state == "ready" and bool(endpoint)
        if ready:
            display_endpoint = endpoint
            if len(display_endpoint) > 54:
                display_endpoint = display_endpoint[:51] + "…"
            target = "Browser terminal" if browser_mode else f"localhost:{port}"
            self.status_item.set_label(f"{display_endpoint}  →  {target}")
            self.set_tunnel_icon("network-vpn-symbolic", "Quick Tunnel ready")
        elif state == "starting":
            message = "Starting browser terminal…" if browser_mode else f"Starting tunnel to localhost:{port}…"
            self.status_item.set_label(message)
            self.set_tunnel_icon("network-transmit-receive-symbolic", "Quick Tunnel starting")
        elif state == "error":
            self.status_item.set_label(response.get("last_error") or "Tunnel error")
            self.set_tunnel_icon("dialog-error-symbolic", "Quick Tunnel error")
        else:
            self.status_item.set_label("No tunnel for this user")
            self.set_tunnel_icon("network-offline-symbolic", "Quick Tunnel stopped")

        self.copy_item.set_sensitive(ready)
        self.copy_browser_item.set_sensitive(ready and bool(self.browser_url))
        self.update_browser_sessions(response.get("browser_sessions", []), ready and browser_mode)
        self.stop_item.set_sensitive(state in {"ready", "starting", "error"})
        self.ssh_item.set_sensitive(True)
        self.browser_item.set_sensitive(True)
        self.custom_item.set_sensitive(True)
        self.refresh_item.set_sensitive(True)

    def apply_error(self, error: str) -> None:
        self.endpoint = None
        self.browser_url = None
        self.last_state = "unavailable"
        self.status_item.set_label(error)
        self.set_tunnel_icon("dialog-error-symbolic", "Quick Tunnel unavailable")
        self.copy_item.set_sensitive(False)
        self.copy_browser_item.set_sensitive(False)
        self.update_browser_sessions([], False)
        self.stop_item.set_sensitive(False)
        self.ssh_item.set_sensitive(True)
        self.browser_item.set_sensitive(True)
        self.custom_item.set_sensitive(True)
        self.refresh_item.set_sensitive(True)
        self.notify("Quick Tunnel error", error, "dialog-error-symbolic")


def acquire_single_instance() -> Any:
    runtime_dir = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    lock_path = runtime_dir / f"{APP_ID}.lock"
    lock_file = lock_path.open("w")
    try:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Quick Tunnel indicator is already running", file=sys.stderr)
        raise SystemExit(0)
    return lock_file


def main() -> int:
    instance_lock = acquire_single_instance()
    indicator = TunnelIndicator()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: Gtk.main_quit())
    Gtk.main()
    del indicator
    del instance_lock
    Notify.uninit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
