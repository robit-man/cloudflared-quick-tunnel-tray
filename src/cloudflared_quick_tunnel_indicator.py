#!/usr/bin/python3
"""Ayatana tray indicator for per-user Cloudflare Quick Tunnels."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import sys
import threading
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


class TunnelIndicator:
    def __init__(self) -> None:
        Notify.init(APP_ID)
        self.endpoint: str | None = None
        self.current_port = 22
        self.busy = False
        self.last_state: str | None = None

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

        self.ssh_item = Gtk.MenuItem(label="Start / cycle SSH tunnel (port 22)")
        self.ssh_item.connect("activate", self.start_ssh)
        self.menu.append(self.ssh_item)

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
        self.quit_item = Gtk.MenuItem(label="Quit indicator")
        self.quit_item.connect("activate", lambda _item: Gtk.main_quit())
        self.menu.append(self.quit_item)

        self.menu.show_all()
        self.indicator.set_menu(self.menu)
        self.refresh()
        GLib.timeout_add_seconds(4, self.periodic_refresh)

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

    def set_busy(self, message: str) -> None:
        self.busy = True
        self.status_item.set_label(message)
        self.indicator.set_icon_full("network-transmit-receive-symbolic", message)
        self.ssh_item.set_sensitive(False)
        self.custom_item.set_sensitive(False)
        self.stop_item.set_sensitive(False)
        self.refresh_item.set_sensitive(False)
        self.copy_item.set_sensitive(False)

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
            self.apply_error(error)
            return False
        assert response is not None
        self.apply_status(response)
        if callback:
            callback(response)
        return False

    def refresh(self) -> None:
        self.run_request({"action": "status"})

    def start_ssh(self, _item: Gtk.MenuItem) -> None:
        self.run_request(
            {"action": "cycle", "port": 22},
            "Creating a new SSH tunnel…",
            self.tunnel_started,
        )

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
        if endpoint:
            self.notify(
                "Quick Tunnel ready",
                f"{endpoint} forwards to localhost:{port}. Use Copy endpoint from the tray menu.",
            )

    def copy_endpoint(self, _item: Gtk.MenuItem) -> None:
        if not self.endpoint:
            return
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        clipboard.set_text(self.endpoint, -1)
        clipboard.store()
        self.notify("Endpoint copied", self.endpoint, "edit-copy-symbolic")

    def apply_status(self, response: dict[str, Any]) -> None:
        state = response.get("state", "unknown")
        endpoint = response.get("endpoint")
        port = response.get("port")
        if isinstance(port, int):
            self.current_port = port
        self.endpoint = endpoint
        self.last_state = state

        ready = state == "ready" and bool(endpoint)
        if ready:
            display_endpoint = endpoint
            if len(display_endpoint) > 54:
                display_endpoint = display_endpoint[:51] + "…"
            self.status_item.set_label(f"{display_endpoint}  →  localhost:{port}")
            self.indicator.set_icon_full("network-vpn-symbolic", "Quick Tunnel ready")
        elif state == "starting":
            self.status_item.set_label(f"Starting tunnel to localhost:{port}…")
            self.indicator.set_icon_full("network-transmit-receive-symbolic", "Quick Tunnel starting")
        elif state == "error":
            self.status_item.set_label(response.get("last_error") or "Tunnel error")
            self.indicator.set_icon_full("dialog-error-symbolic", "Quick Tunnel error")
        else:
            self.status_item.set_label("No tunnel for this user")
            self.indicator.set_icon_full("network-offline-symbolic", "Quick Tunnel stopped")

        self.copy_item.set_sensitive(ready)
        self.stop_item.set_sensitive(state in {"ready", "starting", "error"})
        self.ssh_item.set_sensitive(True)
        self.custom_item.set_sensitive(True)
        self.refresh_item.set_sensitive(True)

    def apply_error(self, error: str) -> None:
        self.endpoint = None
        self.last_state = "unavailable"
        self.status_item.set_label(error)
        self.indicator.set_icon_full("dialog-error-symbolic", "Quick Tunnel unavailable")
        self.copy_item.set_sensitive(False)
        self.stop_item.set_sensitive(False)
        self.ssh_item.set_sensitive(True)
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
    Gtk.main()
    del indicator
    del instance_lock
    Notify.uninit()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
