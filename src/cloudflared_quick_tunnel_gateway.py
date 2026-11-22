#!/usr/bin/python3
"""Bridge an authenticated browser WebSocket to a loopback OpenSSH PTY."""

from __future__ import annotations

import asyncio
import base64
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
from http import HTTPStatus
from pathlib import Path

from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServerProtocol, serve

# The daemon runs this file with python3 -I, which leaves its directory off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloudflared_quick_tunnel_terminal import (  # noqa: E402
    ProtocolError, TerminalSession, control_message, dimensions, resize, start_ssh,
)


TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
HELLO_TIMEOUT = 5
MAX_BROKER_MESSAGE = 16 * 1024
MAX_SESSIONS = 16
MAX_KEY_SIZE = 64 * 1024
SSH = "/usr/bin/ssh"
SSH_HOST = "127.0.0.1"


class TerminalProtocol(WebSocketServerProtocol):
    async def handshake(self, *args, **kwargs):
        # websockets 10 does not yet have the open_timeout option.
        return await asyncio.wait_for(super().handshake(*args, **kwargs), HELLO_TIMEOUT)


async def serve_broker(control: socket.socket, sessions: dict[str, TerminalSession]) -> None:
    control.setblocking(False)
    loop = asyncio.get_running_loop()
    while data := await loop.sock_recv(control, MAX_BROKER_MESSAGE):
        request = json.loads(data)
        ended = False
        if request["action"] == "end":
            session = sessions.get(request["id"])
            if session:
                await session.end_session("Ended by tunnel owner")
                ended = True
        response = {"sessions": [session.status(key) for key, session in sessions.items()], "ended": ended}
        await loop.sock_sendall(control, json.dumps(response).encode("utf-8"))


def authenticate(hello: dict, token: str) -> tuple[str, int, int]:
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


def resume_target(request: dict, sessions: dict[str, TerminalSession]) -> tuple[TerminalSession, int]:
    """Validate a resume request. The per-session secret replaces the tunnel token."""
    if type(request.get("v")) is not int or request["v"] != 1:
        raise ProtocolError("bad_request", "Expected a version 1 terminal resume")
    session_id, secret, offset = request.get("session"), request.get("resume"), request.get("offset")
    session = sessions.get(session_id) if isinstance(session_id, str) else None
    if (
        session is None
        or session.secret is None
        or not isinstance(secret, str)
        or not TOKEN_RE.fullmatch(secret)
        or not secrets.compare_digest(session.secret, secret)
    ):
        raise ProtocolError("expired", "The terminal session is no longer available")
    if type(offset) is not int or offset < 0:
        raise ProtocolError("bad_request", "Invalid output offset")
    return session, offset


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
    sessions: dict[str, TerminalSession] = {}

    async def open_session(websocket: TerminalProtocol, hello: dict):
        user, cols, rows = authenticate(hello, token)
        if len(sessions) >= MAX_SESSIONS:
            raise ProtocolError("busy", "Too many terminal sessions", 1013)
        # Older pages omit "resume" and keep the original one-socket lifetime.
        resumable = hello.get("resume") is True
        pid, fd = start_ssh([*command, "-l", user, "--", SSH_HOST], cols, rows)
        session_id = secrets.token_hex(16)
        session = sessions[session_id] = TerminalSession(
            user, pid, fd, resumable, lambda: sessions.pop(session_id, None),
        )
        ready = {"type": "ready", "v": 1}
        if resumable:
            ready |= {"session": session_id, "resume": session.secret}
        return session, session.claim(websocket, 0), ready

    async def resume_session(websocket: TerminalProtocol, request: dict):
        session, offset = resume_target(request, sessions)
        resize(session.fd, *dimensions(request))
        attachment = session.claim(websocket, offset)
        resumed = {"type": "resumed", "v": 1, "in": session.received, "offset": attachment.position}
        return session, attachment, resumed

    async def handle(websocket: TerminalProtocol) -> None:
        try:
            try:
                message = await asyncio.wait_for(websocket.recv(), HELLO_TIMEOUT)
            except asyncio.TimeoutError:
                raise ProtocolError("timeout", "Timed out waiting for terminal hello") from None
            request = control_message(message)
            start = resume_session if request.get("type") == "resume" else open_session
            session, attachment, header = await start(websocket, request)
            await session.serve(attachment, header)
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
                # Detached sessions have no connection handler to stop their PTYs.
                await asyncio.gather(*(session.close() for session in list(sessions.values())))
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
