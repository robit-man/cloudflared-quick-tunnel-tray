#!/usr/bin/python3
"""Bridge an authenticated browser WebSocket to a loopback OpenSSH PTY."""

from __future__ import annotations

import asyncio
import base64
import ctypes
import errno
import fcntl
import ipaddress
import json
import logging
import os
import re
import secrets
import signal
import socket
import stat
import struct
import sys
import tempfile
import termios
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServerProtocol, serve


TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
HELLO_TIMEOUT = 5
MAX_CONTROL_SIZE = 4096
MAX_BROKER_MESSAGE = 16 * 1024
MAX_SESSIONS = 16
MAX_KEY_SIZE = 64 * 1024
SSH = "/usr/bin/ssh"
SSH_HOST = "127.0.0.1"
PR_SET_PDEATHSIG = 1
LIBC = ctypes.CDLL(None, use_errno=True)


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, close_code: int = 1008) -> None:
        super().__init__(message)
        self.code = code
        self.close_code = close_code


class TerminalProtocol(WebSocketServerProtocol):
    async def handshake(self, *args, **kwargs):
        # websockets 10 does not yet have the open_timeout option.
        return await asyncio.wait_for(super().handshake(*args, **kwargs), HELLO_TIMEOUT)


@dataclass
class BrowserSession:
    websocket: TerminalProtocol
    user: str
    connected_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    finished: asyncio.Event = field(default_factory=asyncio.Event)

    def status(self, session_id: str) -> dict:
        forwarded = self.websocket.request_headers.get_all("CF-Connecting-IP")
        client_ip = None
        if len(forwarded) == 1 and len(forwarded[0]) <= 45 and "%" not in forwarded[0]:
            try:
                client_ip = str(ipaddress.ip_address(forwarded[0]))
            except ValueError:
                pass
        return {"id": session_id, "user": self.user, "connected_at": self.connected_at, "client_ip": client_ip}


async def serve_broker(control: socket.socket, sessions: dict[str, BrowserSession]) -> None:
    control.setblocking(False)
    loop = asyncio.get_running_loop()
    while data := await loop.sock_recv(control, MAX_BROKER_MESSAGE):
        request = json.loads(data)
        ended = False
        if request["action"] == "end":
            session = sessions.get(request["id"])
            if session:
                await session.websocket.close(1000, "Ended by tunnel owner")
                await session.finished.wait()
                ended = True
        response = {"sessions": [session.status(key) for key, session in sessions.items()], "ended": ended}
        await loop.sock_sendall(control, json.dumps(response).encode("utf-8"))


def control_message(message: str | bytes) -> dict:
    if isinstance(message, str) and len(message) <= MAX_CONTROL_SIZE:
        try:
            obj = json.loads(message)
            if isinstance(obj, dict):
                return obj
        except (ValueError, RecursionError):
            pass
    raise ProtocolError("bad_request", "Expected a terminal control message")


def dimensions(message: dict) -> tuple[int, int]:
    cols, rows = message.get("cols"), message.get("rows")
    # The kernel uses two unsigned shorts for the PTY dimensions.
    if any(type(value) is not int or not 1 <= value <= 65535 for value in (cols, rows)):
        raise ProtocolError("bad_request", "Invalid terminal dimensions")
    return cols, rows


def authenticate(message: str | bytes, token: str) -> tuple[str, int, int]:
    hello = control_message(message)
    if hello.get("type") != "hello" or type(hello.get("v")) is not int or hello["v"] != 1:
        raise ProtocolError("bad_request", "Expected a version 1 terminal hello")
    supplied = hello.get("token")
    if (
        not isinstance(supplied, str)
        or not TOKEN_RE.fullmatch(supplied)
        or not secrets.compare_digest(token, supplied)
    ):
        raise ProtocolError("unauthorized", "Invalid terminal token")
    user = hello.get("user")
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise ProtocolError("bad_request", "Invalid SSH username")
    return user, *dimensions(hello)


def encrypted_openssh_key(data: bytes) -> bool:
    lines = data.splitlines()
    if (
        len(lines) < 3
        or lines[0] != b"-----BEGIN OPENSSH PRIVATE KEY-----"
        or lines[-1] != b"-----END OPENSSH PRIVATE KEY-----"
    ):
        return False
    try:
        envelope = base64.b64decode(b"".join(lines[1:-1]), validate=True)
        magic = b"openssh-key-v1\0"
        if not envelope.startswith(magic):
            return False
        cipher_size = struct.unpack_from("!I", envelope, len(magic))[0]
        cipher_start = len(magic) + 4
        cipher_end = cipher_start + cipher_size
        cipher = envelope[cipher_start:cipher_end]
        # OpenSSH requires a nonempty passphrase for encrypted bcrypt keys.
        return bool(
            cipher and cipher != b"none" and envelope[cipher_end:].startswith(b"\0\0\0\x06bcrypt")
        )
    except (ValueError, struct.error):
        return False


def stage_identity(directory: Path) -> Path | None:
    source = Path.home() / ".ssh" / "cloudflared-quick-tunnel"
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as key:
            info = os.fstat(key.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                return None
            data = key.read(MAX_KEY_SIZE + 1)
    except OSError:
        return None
    if len(data) > MAX_KEY_SIZE or not encrypted_openssh_key(data):
        return None
    # Use a private snapshot so the verified file is the one OpenSSH reads.
    identity = directory / "identity"
    with os.fdopen(os.open(identity, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as key:
        key.write(data)
    return identity


def stage_host_keys(directory: Path) -> Path:
    known_hosts = directory / "known_hosts"
    with known_hosts.open("w", encoding="ascii") as destination:
        for key in sorted(Path("/etc/ssh").glob("ssh_host_*_key.pub")):
            try:
                key_type, value, *_ = key.read_bytes().decode("ascii").split()
            except (OSError, UnicodeError, ValueError):
                continue
            destination.write(f"{SSH_HOST} {key_type} {value}\n")
    return known_hosts


def resize(fd: int, cols: int, rows: int) -> None:
    # TIOCSWINSZ sends SIGWINCH to the PTY's foreground process group. OpenSSH
    # relays that window change to the remote PTY.
    fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))


def start_ssh(command: list[str], cols: int, rows: int) -> tuple[int, int]:
    master, slave = os.openpty()
    try:
        resize(slave, cols, rows)
        os.set_blocking(master, False)
        parent = os.getpid()
        pid = os.fork()
    except OSError:
        os.close(master)
        os.close(slave)
        raise
    if pid == 0:
        try:
            # A forced gateway exit must also close detached controlling PTYs.
            death_signal = LIBC.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
            if death_signal != 0 or os.getppid() != parent:
                os._exit(126)
            os.close(master)
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            for fd in (0, 1, 2):
                os.dup2(slave, fd)
            os.close(slave)
            for sig in (signal.SIGPIPE, signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
                signal.signal(sig, signal.SIG_DFL)
            os.execv(SSH, command)
        except OSError:
            os._exit(126)
    os.close(slave)
    return pid, master


async def stop_ssh(pid: int, fd: int) -> None:
    os.close(fd)
    try:
        os.kill(pid, signal.SIGHUP)
    except ProcessLookupError:
        pass
    for _ in range(10):
        if os.waitpid(pid, os.WNOHANG)[0]:
            return
        await asyncio.sleep(0.05)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    os.waitpid(pid, 0)


async def wait_for_fd(fd: int, *, writable: bool = False) -> None:
    loop = asyncio.get_running_loop()
    ready = loop.create_future()
    if writable:
        loop.add_writer(fd, ready.set_result, None)
    else:
        loop.add_reader(fd, ready.set_result, None)
    try:
        await ready
    finally:
        if writable:
            loop.remove_writer(fd)
        else:
            loop.remove_reader(fd)


async def send_output(websocket: TerminalProtocol, fd: int) -> None:
    while True:
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            await wait_for_fd(fd)
            continue
        except OSError as exc:
            if exc.errno == errno.EIO:  # Linux PTY slave closed.
                return
            raise
        if not data:
            return
        await websocket.send(data)
        await asyncio.sleep(0)


async def receive_input(websocket: TerminalProtocol, fd: int) -> None:
    async for message in websocket:
        if isinstance(message, str):
            control = control_message(message)
            if control.get("type") != "resize":
                raise ProtocolError("bad_request", "Expected a terminal resize message")
            resize(fd, *dimensions(control))
            continue
        data = memoryview(message)
        while data:
            try:
                data = data[os.write(fd, data):]
            except BlockingIOError:
                await wait_for_fd(fd, writable=True)


async def relay(websocket: TerminalProtocol, command: list[str], cols: int, rows: int) -> None:
    pid, fd = start_ssh(command, cols, rows)
    tasks = []
    try:
        await websocket.send(json.dumps({"type": "ready", "v": 1}))
        output = asyncio.create_task(send_output(websocket, fd))
        input_ = asyncio.create_task(receive_input(websocket, fd))
        closed = asyncio.create_task(websocket.wait_closed())
        tasks = [output, input_, closed]
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            task.result()
        if output in done:
            await websocket.send(json.dumps({"type": "closed", "message": "SSH session ended"}))
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await stop_ssh(pid, fd)


async def process_request(path, _headers):
    if path != "/ws":
        return HTTPStatus.NOT_FOUND, [], b"Not found\n"
    return None


async def run_gateway(token: str, directory: Path, control: socket.socket) -> None:
    identity = stage_identity(directory)
    known_hosts = stage_host_keys(directory)
    command = [
        SSH, "-F", "none", "-tt", "-e", "none",
        "-o", "StrictHostKeyChecking=yes",
        "-o", f"GlobalKnownHostsFile={known_hosts} /etc/ssh/ssh_known_hosts /etc/ssh/ssh_known_hosts2",
        "-o", "UpdateHostKeys=no",
        "-o", "IdentitiesOnly=yes",
        "-o", f"IdentityFile={identity or 'none'}",
        "-o", "IdentityAgent=none",
        "-o", "PreferredAuthentications=publickey,keyboard-interactive,password",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=30",
    ]
    sessions: dict[str, BrowserSession] = {}

    async def handle(websocket: TerminalProtocol) -> None:
        try:
            try:
                message = await asyncio.wait_for(websocket.recv(), HELLO_TIMEOUT)
            except asyncio.TimeoutError:
                raise ProtocolError("timeout", "Timed out waiting for terminal hello") from None
            user, cols, rows = authenticate(message, token)
            if len(sessions) >= MAX_SESSIONS:
                raise ProtocolError("busy", "Too many terminal sessions", 1013)
            session_id = secrets.token_hex(16)
            session = sessions[session_id] = BrowserSession(websocket, user)
            try:
                await relay(websocket, [*command, "-l", user, "--", SSH_HOST], cols, rows)
            finally:
                del sessions[session_id]
                session.finished.set()
        except ConnectionClosed:
            return
        except OSError:
            await report_error(websocket, ProtocolError("ssh_error", "Terminal I/O failed", 1011))
        except ProtocolError as exc:
            await report_error(websocket, exc)

    stopped = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stopped.set)
    logger = logging.getLogger("terminal-websocket")
    logger.disabled = True  # Never log request headers, protocol frames or terminal bytes.
    try:
        async with serve(
            handle, SSH_HOST, 0, create_protocol=TerminalProtocol, process_request=process_request,
            logger=logger, server_header=None, compression=None, close_timeout=0.5,
            max_size=1024 * 1024, max_queue=4,
        ) as server:
            tasks = {asyncio.create_task(serve_broker(control, sessions)), asyncio.create_task(stopped.wait())}
            try:
                print(server.sockets[0].getsockname()[1], flush=True)
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(sig)


async def report_error(websocket: TerminalProtocol, error: ProtocolError) -> None:
    try:
        await websocket.send(json.dumps({"type": "error", "code": error.code, "message": str(error)}))
        await websocket.close(error.close_code)
    except ConnectionClosed:
        pass


def main() -> int:
    if not os.getuid() or not os.geteuid() or not os.access(SSH, os.X_OK):
        return 1
    token = sys.stdin.readline(45).rstrip("\n")
    if not TOKEN_RE.fullmatch(token):
        return 1
    os.environ["TERM"] = "xterm-256color"
    with socket.socket(fileno=int(sys.argv[1])) as control, tempfile.TemporaryDirectory(
        prefix="quick-tunnel-tty-"
    ) as private_dir:
        control.set_inheritable(False)
        asyncio.run(run_gateway(token, Path(private_dir), control))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
