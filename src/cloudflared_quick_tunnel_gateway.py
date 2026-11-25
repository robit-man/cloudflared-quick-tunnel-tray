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

from cryptography.exceptions import InvalidTag
from websockets.exceptions import ConnectionClosed
from websockets.legacy.server import WebSocketServerProtocol, serve

# The daemon runs this file with python3 -I, which leaves its directory off sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from cloudflared_quick_tunnel_noise import MAX_MESSAGE, SecureSocket, SecureTransportError, respond  # noqa: E402
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
QTTY = "/usr/bin/qtty" if Path(__file__).parent == Path("/usr/libexec") else "/usr/local/bin/qtty"
QTTY_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")


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


def login_parameters(hello: dict) -> tuple[str, int, int]:
    if hello.get("type") != "hello" or type(hello.get("v")) is not int or hello["v"] != 1:
        raise ProtocolError("bad_request", "Expected a version 1 terminal hello")
    user = hello.get("user")
    if not isinstance(user, str) or not USER_RE.fullmatch(user):
        raise ProtocolError("bad_request", "Invalid SSH username")
    return user, *dimensions(hello)


def remote_command(request: dict) -> list[str]:
    """Land browser logins in qtty so their terminals outlive the browser.

    "attach" returns to a named session; "fork" starts a new one in a named
    session's directory. Names are validated, so they are safe in the remote
    shell command line.
    """
    attach, fork = request.get("attach"), request.get("fork")
    for name in (attach, fork):
        if name is not None and (not isinstance(name, str) or not QTTY_NAME_RE.fullmatch(name)):
            raise ProtocolError("bad_request", "Invalid qtty session name")
    if not os.access(QTTY, os.X_OK):
        return []
    if fork is not None:
        return [QTTY, "login", "--fork", fork]
    return [QTTY, "login", attach] if attach is not None else [QTTY, "login"]


def owned_session(request: dict, sessions: dict[str, TerminalSession]) -> TerminalSession:
    """Find the session a resume or fork request proves it owns."""
    if type(request.get("v")) is not int or request["v"] != 1:
        raise ProtocolError("bad_request", "Expected a version 1 terminal request")
    session_id, secret = request.get("session"), request.get("resume")
    session = sessions.get(session_id) if isinstance(session_id, str) else None
    if (
        session is None
        or session.secret is None
        or not isinstance(secret, str)
        or not TOKEN_RE.fullmatch(secret)
        or not secrets.compare_digest(session.secret, secret)
    ):
        raise ProtocolError("expired", "The terminal session is no longer available")
    return session


def resume_target(request: dict, sessions: dict[str, TerminalSession]) -> tuple[TerminalSession, int]:
    """Validate a resume request. The per-session secret replaces the tunnel token."""
    session = owned_session(request, sessions)
    offset = request.get("offset")
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
    if path != "/ws/v2":
        return HTTPStatus.NOT_FOUND, [], b"Not found\n"
    return None


async def run_gateway(token: str, directory: Path, control: socket.socket | None, *, publish=None) -> None:
    psk = base64.urlsafe_b64decode(token + "=")
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

    def launch(websocket, user, cols, rows, remote, resumable, control):
        if len(sessions) >= MAX_SESSIONS:
            raise ProtocolError("busy", "Too many terminal sessions", 1013)
        session_id = secrets.token_hex(16)
        # The first login becomes an OpenSSH master; forks reuse its
        # authentication. If the master is gone, ssh simply asks again.
        control = control or directory / f"cm-{session_id}"
        multiplex = ["-o", "ControlMaster=auto", "-o", f"ControlPath={control}", "-o", "ControlPersist=60"]
        pid, fd = start_ssh([*command, *multiplex, "-l", user, "--", SSH_HOST, *remote], cols, rows)
        session = sessions[session_id] = TerminalSession(
            user, pid, fd, resumable, lambda: sessions.pop(session_id, None),
        )
        session.control_path = control
        ready = {"type": "ready", "v": 1}
        if resumable:
            ready |= {"session": session_id, "resume": session.secret}
        return session, session.claim(websocket, 0), ready

    async def open_session(websocket: SecureSocket, hello: dict):
        user, cols, rows = login_parameters(hello)
        return launch(websocket, user, cols, rows, remote_command(hello), hello.get("resume") is True, None)

    async def fork_session(websocket: SecureSocket, request: dict):
        parent = owned_session(request, sessions)
        cols, rows = dimensions(request)
        return launch(websocket, parent.user, cols, rows, remote_command(request), True, parent.control_path)

    async def resume_session(websocket: SecureSocket, request: dict):
        session, offset = resume_target(request, sessions)
        resize(session.fd, *dimensions(request))
        attachment = session.claim(websocket, offset)
        resumed = {"type": "resumed", "v": 1, "in": session.received, "offset": attachment.position}
        return session, attachment, resumed

    async def handle(relay: TerminalProtocol) -> None:
        try:
            handshake = await asyncio.wait_for(relay.recv(), HELLO_TIMEOUT)
            reply, incoming, outgoing = respond(psk, handshake)
            await relay.send(reply)
        except (asyncio.TimeoutError, ValueError, InvalidTag):
            await relay.close(1008)
            return
        except ConnectionClosed:
            return
        websocket = SecureSocket(relay, incoming, outgoing)
        try:
            try:
                message = await asyncio.wait_for(websocket.recv(), HELLO_TIMEOUT)
            except asyncio.TimeoutError:
                raise ProtocolError("timeout", "Timed out waiting for terminal hello") from None
            request = control_message(message)
            start = {"resume": resume_session, "fork": fork_session}.get(request.get("type"), open_session)
            session, attachment, header = await start(websocket, request)
            await session.serve(attachment, header)
        except ConnectionClosed:
            return
        except SecureTransportError:
            await relay.close(1008)
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
            max_size=MAX_MESSAGE, max_queue=4,
        ) as server:
            port = server.sockets[0].getsockname()[1]
            tasks = {asyncio.create_task(stopped.wait())}
            if control is not None:
                tasks.add(asyncio.create_task(serve_broker(control, sessions)))
            if publish is not None:
                tasks.add(asyncio.create_task(publish(port)))
            try:
                if publish is None:
                    print(port, flush=True)
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


async def report_error(websocket: SecureSocket, error: ProtocolError) -> None:
    try:
        await websocket.send(json.dumps({"type": "error", "code": error.code, "message": str(error)}))
        await websocket.close(error.close_code)
    except ConnectionClosed:
        pass


async def publish_desktop(port: int, cloudflared: str) -> None:
    loop = asyncio.get_running_loop()
    eof = asyncio.StreamReader()
    pipe, _ = await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(eof), sys.stdin.buffer)
    try:
        tunnel = await asyncio.create_subprocess_exec(
            cloudflared, "tunnel", "--config", os.devnull, "--no-autoupdate", "--metrics", "localhost:0",
            "--url", f"http://127.0.0.1:{port}", stdin=asyncio.subprocess.DEVNULL,
        )
        tasks = [asyncio.create_task(eof.read()), asyncio.create_task(tunnel.wait())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if tunnel.returncode is None:
                try:
                    tunnel.terminate()
                    await asyncio.wait_for(tunnel.wait(), 3)
                except ProcessLookupError:
                    pass
                except asyncio.TimeoutError:
                    tunnel.kill()
                    await tunnel.wait()
    finally:
        pipe.close()


def main(args: list[str] | None = None) -> int:
    args = sys.argv[1:] if args is None else args
    if not os.getuid() or not os.geteuid() or not os.access(SSH, os.X_OK):
        return 1
    token = sys.stdin.buffer.readline(45).rstrip(b"\n").decode("ascii", errors="replace")
    if not TOKEN_RE.fullmatch(token) or not args:
        return 1
    os.environ["TERM"] = "xterm-256color"
    # A short path leaves space for OpenSSH's control sockets, including on macOS.
    with tempfile.TemporaryDirectory(prefix="quick-tunnel-tty-", dir="/tmp") as private_dir:
        directory = Path(private_dir)
        if len(args) == 2 and args[0] == "--desktop":
            with socket.socket() as ssh:
                ssh.settimeout(1)
                if ssh.connect_ex((SSH_HOST, 22)):
                    print("No local SSH server is accepting connections on port 22.", flush=True)
                    return 1
            asyncio.run(run_gateway(token, directory, None, publish=lambda port: publish_desktop(port, args[1])))
        else:
            with socket.socket(fileno=int(args[0])) as control:
                control.set_inheritable(False)
                asyncio.run(run_gateway(token, directory, control))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
