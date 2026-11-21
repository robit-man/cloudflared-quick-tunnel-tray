#!/usr/bin/python3
"""Root broker for per-user Cloudflare Quick Tunnels.

The broker authenticates local clients with SO_PEERCRED and keeps exactly one
tunnel per Unix UID.  Tunnel processes run as the requesting user and may only
target a TCP port on this machine.
"""

from __future__ import annotations

import collections
import json
import logging
import os
import pwd
import re
import secrets
import select
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


VERSION = "1.0.0"
SOCKET_PATH = Path("/run/cloudflared-quick-tunnel/control.sock")
EXECUTABLE_PATH = "/usr/local/bin:/usr/bin:/bin"
CLOUDFLARED = shutil.which("cloudflared", path=EXECUTABLE_PATH) or "/usr/local/bin/cloudflared"
SETPRIV = shutil.which("setpriv", path=EXECUTABLE_PATH) or "/usr/bin/setpriv"
GATEWAY = Path(__file__).with_name("cloudflared_quick_tunnel_gateway.py")
NATIVE_MODE = "native"
BROWSER_MODE = "browser-tty"
ENDPOINT_RE = re.compile(r"https://([a-z0-9-]+\.trycloudflare\.com)\b", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
MAX_REQUEST = 16 * 1024
START_TIMEOUT = 40.0


LOG = logging.getLogger("cloudflared-quick-tunnel")


class RequestError(Exception):
    """A safe error that can be returned to a local client."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def local_port_is_open(port: int) -> bool:
    """Return true when a TCP listener accepts on IPv4 or IPv6 loopback."""
    for family, address in (
        (socket.AF_INET, ("127.0.0.1", port)),
        (socket.AF_INET6, ("::1", port, 0, 0)),
    ):
        sock = socket.socket(family, socket.SOCK_STREAM)
        sock.settimeout(0.75)
        try:
            if sock.connect_ex(address) == 0:
                return True
        except OSError:
            pass
        finally:
            sock.close()
    return False


def local_ssh_is_open() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", 22), timeout=0.75):
            return True
    except OSError:
        return False


@dataclass
class TunnelSession:
    uid: int
    gid: int
    username: str
    port: int | None = None
    mode: str = NATIVE_MODE
    process: subprocess.Popen[str] | None = None
    gateway: subprocess.Popen[bytes] | None = None
    token: str | None = field(default=None, repr=False)
    state: str = "stopped"
    endpoint: str | None = None
    assigned_endpoint: str | None = None
    registered: bool = False
    started_at: str | None = None
    last_error: str | None = None
    logs: collections.deque[str] = field(
        default_factory=lambda: collections.deque(maxlen=60)
    )
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self.changed = threading.Condition(self.lock)

    def status(self) -> dict[str, Any]:
        with self.lock:
            self._notice_exit_locked()
            return self._status_locked()

    def _status_locked(self) -> dict[str, Any]:
        pid = self.process.pid if self.process and self.process.poll() is None else None
        return {
            "version": VERSION,
            "owner_uid": self.uid,
            "owner_user": self.username,
            "state": self.state,
            "mode": self.mode,
            "port": self.port,
            "endpoint": self.endpoint,
            "endpoint_url": f"https://{self.endpoint}" if self.endpoint else None,
            "browser_url": (
                f"https://{self.endpoint}/#tty={self.token}"
                if self.state == "ready" and self.endpoint and self.token else None
            ),
            "pid": pid,
            "started_at": self.started_at,
            "last_error": self.last_error,
        }

    def start(self, port: int, force: bool, mode: str = NATIVE_MODE) -> dict[str, Any]:
        with self.changed:
            if STOP_EVENT.is_set():
                raise RequestError("Quick Tunnel service is shutting down")
            self._notice_exit_locked()
            if (
                not force
                and self.process
                and self.process.poll() is None
                and self.port == port
                and self.mode == mode
                and self.state == "ready"
            ):
                return self._status_locked()

            if mode == BROWSER_MODE:
                if self.uid == 0:
                    raise RequestError("Browser terminal mode requires a non-root Unix user")
                if not local_ssh_is_open():
                    raise RequestError("No SSH service is accepting connections on 127.0.0.1:22")
            elif not local_port_is_open(port):
                raise RequestError(
                    f"No TCP service is accepting connections on localhost:{port}"
                )

            self._stop_locked()
            self.mode = mode
            self.port = port
            pw_entry = pwd.getpwuid(self.uid)
            drop_privileges = [
                SETPRIV,
                "--reuid",
                str(self.uid),
                "--regid",
                str(self.gid),
                "--init-groups",
                "--no-new-privs",
            ]
            environment = {
                "HOME": pw_entry.pw_dir,
                "LOGNAME": self.username,
                "PATH": EXECUTABLE_PATH,
                "USER": self.username,
            }
            scheme = "ssh" if port == 22 else "tcp"
            origin = f"{scheme}://127.0.0.1:{port}"
            if mode == BROWSER_MODE:
                try:
                    gateway_port = self._start_gateway_locked(drop_privileges, environment)
                except RequestError as exc:
                    self._fail_locked(str(exc))
                    raise
                origin = f"http://127.0.0.1:{gateway_port}"
            try:
                process = subprocess.Popen(
                    [
                        *drop_privileges, CLOUDFLARED, "tunnel", "--config", "/dev/null",
                        "--no-autoupdate", "--metrics", "localhost:0", "--url", origin,
                    ],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    close_fds=True,
                    env=environment,
                    start_new_session=True,
                )
            except OSError as exc:
                message = f"Unable to start cloudflared: {exc}"
                self._fail_locked(message)
                raise RequestError(message) from exc

            self.process = process
            self.state = "starting"
            self.started_at = utc_now()
            self.logs.clear()
            LOG.info(
                "starting tunnel uid=%s user=%s mode=%s port=%s pid=%s",
                self.uid,
                self.username,
                self.mode,
                port,
                process.pid,
            )
            threading.Thread(
                target=self._read_output,
                args=(process,),
                name=f"tunnel-output-{self.uid}",
                daemon=True,
            ).start()

            deadline = time.monotonic() + START_TIMEOUT
            while self.process is process and self.state == "starting" and time.monotonic() < deadline:
                self.changed.wait(timeout=min(1.0, deadline - time.monotonic()))
                self._notice_exit_locked()

            if self.process is process and self.state == "ready":
                return self._status_locked()

            if self.process is process and self.state == "starting":
                self._fail_locked("Timed out waiting for the Quick Tunnel to register")
            raise RequestError(self.last_error or "Tunnel start was interrupted")

    def _start_gateway_locked(self, drop_privileges: list[str], environment: dict[str, str]) -> int:
        token = secrets.token_urlsafe(32)
        try:
            gateway = subprocess.Popen(
                [*drop_privileges, "/usr/bin/python3", "-I", str(GATEWAY)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                env=environment,
                start_new_session=True,
            )
            self.gateway = gateway
            assert gateway.stdin is not None and gateway.stdout is not None
            with gateway.stdin as token_pipe, gateway.stdout as ready_pipe:
                token_pipe.write(token.encode("ascii") + b"\n")
                token_pipe.flush()
                if not select.select([ready_pipe], [], [], 3)[0]:
                    raise RequestError("Timed out starting the browser terminal gateway")
                announcement = os.read(ready_pipe.fileno(), 6)
                port = int(announcement)
                if not announcement.endswith(b"\n") or not 1 <= port <= 65535:
                    raise ValueError("Invalid gateway port")
        except (OSError, ValueError) as exc:
            raise RequestError("Unable to start the browser terminal gateway") from exc
        self.token = token
        threading.Thread(
            target=self._watch_gateway,
            args=(gateway,),
            name=f"terminal-gateway-{self.uid}",
            daemon=True,
        ).start()
        return port

    def _watch_gateway(self, gateway: subprocess.Popen[bytes]) -> None:
        gateway.wait()
        with self.changed:
            if gateway is self.gateway:
                self._notice_exit_locked()

    def stop(self) -> dict[str, Any]:
        with self.changed:
            self._stop_locked()
            return self._status_locked()

    def _stop_locked(self) -> None:
        if self.process:
            LOG.info("stopping tunnel uid=%s pid=%s", self.uid, self.process.pid)
        self._clear_processes_locked()
        self.state = "stopped"
        self.started_at = None
        self.last_error = None
        self.changed.notify_all()

    def _clear_processes_locked(self) -> None:
        # Clear ownership before waiting so stale watchers cannot update the next tunnel.
        gateway, process = self.gateway, self.process
        self.gateway = None
        self.process = None
        self.token = None
        self.endpoint = None
        self.assigned_endpoint = None
        self.registered = False
        for child in (gateway, process):
            if child and child.poll() is None:
                self._terminate_process_locked(child)

    def _fail_locked(self, message: str) -> None:
        self._clear_processes_locked()
        self.state = "error"
        self.last_error = message
        self.changed.notify_all()

    @staticmethod
    def _terminate_process_locked(process: subprocess.Popen) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=6)
            return
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            LOG.error("tunnel process group did not exit pid=%s", process.pid)

    def _read_output(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                line = ANSI_RE.sub("", raw_line).strip()
                if not line:
                    continue
                with self.changed:
                    if process is not self.process:
                        continue
                    self.logs.append(line[-1000:])
                    endpoint_match = ENDPOINT_RE.search(line)
                    if endpoint_match:
                        self.assigned_endpoint = endpoint_match.group(1).lower()
                    if "Registered tunnel connection" in line:
                        self.registered = True
                    if self.assigned_endpoint and self.registered:
                        self._notice_exit_locked()
                        if process is not self.process:
                            continue
                        self.endpoint = self.assigned_endpoint
                        self.state = "ready"
                        self.changed.notify_all()
        finally:
            process.stdout.close()
            process.wait()
            with self.changed:
                if process is self.process:
                    self._notice_exit_locked()

    def _notice_exit_locked(self) -> None:
        if self.gateway and self.gateway.poll() is not None:
            LOG.warning("browser terminal gateway exited uid=%s status=%s", self.uid, self.gateway.returncode)
            self._fail_locked("Browser terminal gateway exited unexpectedly")
        elif self.process and self.process.poll() is not None:
            status = self.process.returncode
            tail = self.logs[-1] if self.logs else "no diagnostic output"
            LOG.warning("tunnel exited uid=%s pid=%s status=%s", self.uid, self.process.pid, status)
            self._fail_locked(f"cloudflared exited with status {status}: {tail}")


class TunnelManager:
    def __init__(self) -> None:
        self.sessions: dict[int, TunnelSession] = {}
        self.lock = threading.Lock()

    def session_for_peer(self, uid: int, gid: int) -> TunnelSession:
        try:
            pw_entry = pwd.getpwuid(uid)
        except KeyError as exc:
            raise RequestError(f"Unknown local UID {uid}") from exc
        with self.lock:
            session = self.sessions.get(uid)
            if session is None:
                session = TunnelSession(uid, pw_entry.pw_gid, pw_entry.pw_name)
                self.sessions[uid] = session
            return session

    def stop_all(self) -> None:
        with self.lock:
            sessions = list(self.sessions.values())
        for session in sessions:
            session.stop()


MANAGER = TunnelManager()
STOP_EVENT = threading.Event()


def validate_port(value: Any) -> int:
    if isinstance(value, bool):
        raise RequestError("Port must be an integer from 1 through 65535")
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise RequestError("Port must be an integer from 1 through 65535") from exc
    if not 1 <= port <= 65535:
        raise RequestError("Port must be an integer from 1 through 65535")
    return port


def handle_request(uid: int, gid: int, request: dict[str, Any]) -> dict[str, Any]:
    action = request.get("action")
    if action == "ping":
        return {"ok": True, "version": VERSION}

    session = MANAGER.session_for_peer(uid, gid)
    if action == "status":
        return {"ok": True, **session.status()}
    if action in ("start", "cycle"):
        with session.lock:
            keep_current = action == "cycle" and "mode" not in request and "port" not in request
            mode = session.mode if keep_current else request.get("mode", NATIVE_MODE)
            if mode not in (NATIVE_MODE, BROWSER_MODE):
                raise RequestError("Unknown tunnel mode")
            if mode == BROWSER_MODE and "port" in request:
                raise RequestError("Browser terminal mode uses the local SSH service on port 22")
            default_port = (session.port or 22) if keep_current else 22
            port = validate_port(request.get("port", default_port))
            return {"ok": True, **session.start(port, force=action == "cycle", mode=mode)}
    if action == "stop":
        return {"ok": True, **session.stop()}
    raise RequestError("Unknown action; use status, start, cycle, stop, or ping")


def peer_credentials(connection: socket.socket) -> tuple[int, int, int]:
    data = connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
    pid, uid, gid = struct.unpack("3i", data)
    return pid, uid, gid


def serve_connection(connection: socket.socket) -> None:
    try:
        peer_pid, peer_uid, peer_gid = peer_credentials(connection)
        connection.settimeout(50)
        data = bytearray()
        while b"\n" not in data:
            chunk = connection.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
            if len(data) > MAX_REQUEST:
                raise RequestError("Request is too large")
        if not data:
            return
        try:
            request = json.loads(bytes(data).split(b"\n", 1)[0].decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RequestError("Request must be one line of JSON") from exc
        if not isinstance(request, dict):
            raise RequestError("Request must be a JSON object")
        LOG.debug("request pid=%s uid=%s action=%s", peer_pid, peer_uid, request.get("action"))
        response = handle_request(peer_uid, peer_gid, request)
    except RequestError as exc:
        response = {"ok": False, "error": str(exc)}
    except Exception:
        LOG.exception("unexpected request failure")
        response = {"ok": False, "error": "Internal service error; check the system journal"}
    try:
        connection.sendall(json.dumps(response, separators=(",", ":")).encode() + b"\n")
    except OSError:
        pass
    finally:
        connection.close()


def request_shutdown(signum: int, _frame: Any) -> None:
    LOG.info("received signal %s; stopping all tunnels", signum)
    STOP_EVENT.set()


def main() -> int:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if os.geteuid() != 0:
        LOG.error("this broker must run as root so it can drop tunnel privileges per user")
        return 1
    for required_path in (CLOUDFLARED, SETPRIV):
        if not os.path.isfile(required_path) or not os.access(required_path, os.X_OK):
            LOG.error("required executable is missing: %s", required_path)
            return 1

    SOCKET_PATH.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    try:
        SOCKET_PATH.unlink()
    except FileNotFoundError:
        pass

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(SOCKET_PATH))
    os.chmod(SOCKET_PATH, 0o666)
    listener.listen(32)
    listener.settimeout(1.0)

    signal.signal(signal.SIGTERM, request_shutdown)
    signal.signal(signal.SIGINT, request_shutdown)
    LOG.info("broker ready version=%s socket=%s", VERSION, SOCKET_PATH)
    try:
        while not STOP_EVENT.is_set():
            try:
                connection, _ = listener.accept()
            except socket.timeout:
                continue
            threading.Thread(
                target=serve_connection,
                args=(connection,),
                name="client-request",
                daemon=True,
            ).start()
    finally:
        listener.close()
        MANAGER.stop_all()
        try:
            SOCKET_PATH.unlink()
        except FileNotFoundError:
            pass
        LOG.info("broker stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
