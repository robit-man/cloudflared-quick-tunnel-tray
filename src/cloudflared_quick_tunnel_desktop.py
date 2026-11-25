"""User-session tray for macOS and Windows."""

from __future__ import annotations

import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path


SITE = "https://robit-man.github.io/tty/"
ENDPOINT = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)\b", re.IGNORECASE)
BUNDLE = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
WINDOWS = sys.platform == "win32"
PROCESS_FLAGS = subprocess.CREATE_NO_WINDOW if WINDOWS else 0
CLOUDFLARED = BUNDLE / ("cloudflared.exe" if WINDOWS else "cloudflared")
WSL = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "wsl.exe"


def browser_command() -> list[str]:
    if not WINDOWS:
        launcher = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, __file__]
        return [*launcher, "--desktop", str(CLOUDFLARED)]

    # Both SSH and cloudflared run in the default WSL distribution. This keeps
    # the gateway bound to its own loopback on every WSL networking mode.
    try:
        directory = subprocess.check_output(
            [str(WSL), "--exec", "wslpath", "-a", str(BUNDLE / "wsl")],
            stderr=subprocess.DEVNULL, creationflags=PROCESS_FLAGS, timeout=15,
        ).decode("utf-8").strip()
    except (OSError, subprocess.SubprocessError, UnicodeError) as exc:
        raise RuntimeError("Install WSL 2 and initialize a Linux distribution to start Browser Terminal.") from exc
    return [
        str(WSL), "--exec", "/usr/bin/python3", "-I",
        f"{directory}/cloudflared_quick_tunnel_gateway.py", "--desktop", f"{directory}/cloudflared",
    ]


def stop_process(process: subprocess.Popen) -> None:
    if process.stdin is not None:
        process.stdin.close()  # The gateway supervises cloudflared until EOF.
    elif process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class Tray:
    def __init__(self, icon) -> None:
        self.icon = icon
        self.lock = threading.Lock()
        self.process = None
        self.endpoint = ""
        self.link = ""
        self.busy = False

    def update(self, title: str) -> None:
        self.icon.title = title
        self.icon.update_menu()

    def start(self, browser: bool) -> None:
        if self.busy:
            return
        self.busy = True
        self.update("Starting Quick Tunnel…")
        threading.Thread(target=self.run, args=(browser,), daemon=True).start()

    def run(self, browser: bool) -> None:
        with self.lock:
            self.stop_locked()
            token = secrets.token_urlsafe(32) if browser else ""
            ready = threading.Event()
            try:
                if browser:
                    command = browser_command()
                else:
                    try:
                        with socket.create_connection(("127.0.0.1", 22), timeout=1):
                            pass
                    except OSError:
                        raise RuntimeError("Start a local SSH server on port 22 first.") from None
                    command = [str(CLOUDFLARED), "tunnel", "--config", os.devnull, "--no-autoupdate",
                               "--metrics", "localhost:0", "--url", "ssh://127.0.0.1:22"]
                process = subprocess.Popen(
                    command, stdin=subprocess.PIPE if browser else subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=PROCESS_FLAGS,
                )
                self.process = process
                if process.stdin:
                    process.stdin.write(token.encode("ascii") + b"\n")
                    process.stdin.flush()
                threading.Thread(target=self.read, args=(process, token, ready), daemon=True).start()
            except (OSError, RuntimeError) as exc:
                self.stop_locked()
                self.busy = False
                self.update(str(exc) if isinstance(exc, RuntimeError) else "Could not start Quick Tunnel")
                return
        if not ready.wait(45):
            with self.lock:
                if self.process is process:
                    self.stop_locked()
                    self.busy = False
                    self.update("Quick Tunnel did not register in time")

    def read(self, process, token: str, ready: threading.Event) -> None:
        endpoint = ""
        registered = False
        detail = ""
        with process.stdout:
            for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="replace")
                match = ENDPOINT.search(line)
                if match:
                    endpoint = match.group(1).lower()
                registered |= "Registered tunnel connection" in line
                if "No local SSH server" in line:
                    detail = "Start a local SSH server on port 22 first"
                if "No module named" in line and WINDOWS:
                    detail = "Install python3-cryptography and python3-websockets in WSL"
                if endpoint and registered and not ready.is_set():
                    with self.lock:
                        if self.process is not process:
                            return
                        self.endpoint = endpoint
                        self.link = f"https://{endpoint}/#tty=v2.{token}" if token else ""
                        self.busy = False
                        ready.set()
                        self.update(endpoint)
        process.wait()
        ready.set()
        with self.lock:
            if self.process is process:
                self.stop_locked()
                self.busy = False
                self.update(detail or "Quick Tunnel closed")

    def stop_locked(self) -> None:
        process, self.process = self.process, None
        self.endpoint = self.link = ""
        if process is not None:
            stop_process(process)

    def stop(self) -> None:
        with self.lock:
            self.stop_locked()
            self.busy = False
            self.update("Quick Tunnel stopped")

    def quit(self) -> None:
        self.stop()
        self.icon.stop()


def main() -> int:
    if sys.argv[1:2] == ["--desktop"] and not WINDOWS:
        from cloudflared_quick_tunnel_gateway import main as gateway_main

        return gateway_main()

    import pyperclip
    import pystray
    from PIL import Image, ImageDraw

    if sys.argv[1:] == ["--self-test"]:
        if not WINDOWS:
            from cloudflared_quick_tunnel_gateway import respond

            assert callable(respond)
        return subprocess.call([str(CLOUDFLARED), "--version"], creationflags=PROCESS_FLAGS)

    image = Image.new("RGBA", (64, 64))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((3, 8, 61, 56), radius=8, fill=(24, 26, 28, 255), outline="white", width=3)
    draw.line([(15, 22), (25, 31), (15, 40)], fill="white", width=4)
    draw.line([(31, 40), (48, 40)], fill="white", width=4)
    icon = pystray.Icon("cloudflared-quick-tunnel-tray", image, "Quick Tunnel stopped")
    tray = Tray(icon)
    item = pystray.MenuItem
    icon.menu = pystray.Menu(
        item(lambda _: icon.title, lambda: None, enabled=False),
        pystray.Menu.SEPARATOR,
        item("Start / cycle browser terminal", lambda: tray.start(True), enabled=lambda _: not tray.busy),
        item("Start / cycle SSH tunnel (port 22)", lambda: tray.start(False), enabled=lambda _: not tray.busy),
        item("Copy browser terminal link", lambda: pyperclip.copy(tray.link), enabled=lambda _: bool(tray.link)),
        item("Copy endpoint", lambda: pyperclip.copy(tray.endpoint), enabled=lambda _: bool(tray.endpoint)),
        item("Open TTY", lambda: webbrowser.open(SITE)),
        item("Stop tunnel", tray.stop, enabled=lambda _: bool(tray.process) or tray.busy),
        pystray.Menu.SEPARATOR,
        item("Quit", tray.quit),
    )
    try:
        icon.run()
    finally:
        tray.stop_locked()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
