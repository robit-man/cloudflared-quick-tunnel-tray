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


@dataclass
class TunnelSession:
    uid: int
    gid: int
    username: str
    port: int | None = None
    process: subprocess.Popen[str] | None = None
    state: str = "stopped"
    endpoint: str | None = None
    assigned_endpoint: str | None = None
    registered: bool = False
    started_at: str | None = None
    last_error: str | None = None
    stop_requested: bool = False
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
            "port": self.port,
            "endpoint": self.endpoint,
            "endpoint_url": f"https://{self.endpoint}" if self.endpoint else None,
            "pid": pid,
            "started_at": self.started_at,
            "last_error": self.last_error,
        }

    def start(self, port: int, force: bool) -> dict[str, Any]:
        with self.changed:
            self._notice_exit_locked()
            if (
                not force
                and self.process
                and self.process.poll() is None
                and self.port == port
                and self.state == "ready"
            ):
                return self._status_locked()

            if not local_port_is_open(port):
                raise RequestError(
                    f"No TCP service is accepting connections on localhost:{port}"
                )

            self._stop_locked()
            scheme = "ssh" if port == 22 else "tcp"
            pw_entry = pwd.getpwuid(self.uid)
            command = [
                SETPRIV,
                "--reuid",
                str(self.uid),
                "--regid",
                str(self.gid),
                "--init-groups",
                "--no-new-privs",
                CLOUDFLARED,
                "tunnel",
                "--config",
                "/dev/null",
                "--no-autoupdate",
                "--metrics",
                "localhost:0",
                "--url",
                f"{scheme}://127.0.0.1:{port}",
            ]
            environment = {
                "HOME": pw_entry.pw_dir,
                "LOGNAME": self.username,
                "PATH": EXECUTABLE_PATH,
                "USER": self.username,
            }
            try:
                process = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    bufsize=1,
                    close_fds=True,
                    env=environment,
                    start_new_session=True,
                )
            except OSError as exc:
                raise RequestError(f"Unable to start cloudflared: {exc}") from exc

            self.port = port
            self.process = process
            self.state = "starting"
            self.endpoint = None
            self.assigned_endpoint = None
            self.registered = False
            self.started_at = utc_now()
            self.last_error = None
            self.stop_requested = False
            self.logs.clear()
            LOG.info(
                "starting tunnel uid=%s user=%s port=%s pid=%s",
                self.uid,
                self.username,
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
            while self.state == "starting" and time.monotonic() < deadline:
                self.changed.wait(timeout=min(1.0, deadline - time.monotonic()))
                self._notice_exit_locked()

            if self.state == "ready":
                return self._status_locked()

            if self.state == "starting":
                self.last_error = "Timed out waiting for the Quick Tunnel to register"
                self._terminate_process_locked(process)
                self.process = None
                self.state = "error"
            raise RequestError(self.last_error or "cloudflared did not become ready")

    def stop(self) -> dict[str, Any]:
        with self.changed:
            self._stop_locked()
            return self._status_locked()

    def _stop_locked(self) -> None:
        process = self.process
        if process and process.poll() is None:
            self.stop_requested = True
            LOG.info(
                "stopping tunnel uid=%s user=%s port=%s pid=%s",
                self.uid,
                self.username,
                self.port,
                process.pid,
            )
            self._terminate_process_locked(process)
        self.process = None
        self.state = "stopped"
        self.endpoint = None
        self.assigned_endpoint = None
        self.registered = False
        self.started_at = None
        self.last_error = None
        self.stop_requested = False
        self.changed.notify_all()

    @staticmethod
    def _terminate_process_locked(process: subprocess.Popen[str]) -> None:
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
                        self.endpoint = self.assigned_endpoint
                        self.state = "ready"
                        self.changed.notify_all()
        finally:
            return_code = process.wait()
            with self.changed:
                if process is self.process:
                    if self.stop_requested:
                        self.state = "stopped"
                        self.last_error = None
                    else:
                        self.state = "error"
                        tail = self.logs[-1] if self.logs else "no diagnostic output"
                        self.last_error = (
                            f"cloudflared exited with status {return_code}: {tail}"
                        )
                        LOG.warning(
                            "tunnel exited uid=%s pid=%s status=%s",
                            self.uid,
                            process.pid,
                            return_code,
                        )
                    self.process = None
                    self.endpoint = None
                    self.changed.notify_all()

    def _notice_exit_locked(self) -> None:
        if self.process and self.process.poll() is not None and self.state in {
            "starting",
            "ready",
        }:
            return_code = self.process.returncode
            self.process = None
            self.endpoint = None
            self.state = "error"
            self.last_error = f"cloudflared exited with status {return_code}"


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
    if action == "start":
        port = validate_port(request.get("port", 22))
        return {"ok": True, **session.start(port, force=False)}
    if action == "cycle":
        current_port = session.status().get("port") or 22
        port = validate_port(request.get("port", current_port))
        return {"ok": True, **session.start(port, force=True)}
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
