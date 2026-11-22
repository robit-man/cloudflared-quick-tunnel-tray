"""Keep a loopback OpenSSH PTY alive while its browser WebSocket reconnects.

A browser that moves between networks (for example Wi-Fi to cellular) loses
its TCP connection to Cloudflare without a clean close. A resumable session
keeps the PTY running, buffers recent output by absolute byte offset, and lets
the browser attach again with its per-session resume secret. Input bytes are
counted so the browser can resend only what the PTY never received.
"""

from __future__ import annotations

import asyncio
import ctypes
import errno
import fcntl
import ipaddress
import json
import os
import secrets
import signal
import struct
import termios
from datetime import datetime, timezone
from typing import Callable

from websockets.exceptions import ConnectionClosed


MAX_CONTROL_SIZE = 4096
OUTPUT_BUFFER = 1024 * 1024
DETACH_TIMEOUT = 15 * 60
READ_SIZE = 65536
PR_SET_PDEATHSIG = 1
LIBC = ctypes.CDLL(None, use_errno=True)


class ProtocolError(Exception):
    def __init__(self, code: str, message: str, close_code: int = 1008) -> None:
        super().__init__(message)
        self.code = code
        self.close_code = close_code


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


def client_ip(websocket) -> str | None:
    forwarded = websocket.request_headers.get_all("CF-Connecting-IP")
    if len(forwarded) == 1 and len(forwarded[0]) <= 45 and "%" not in forwarded[0]:
        try:
            return str(ipaddress.ip_address(forwarded[0]))
        except ValueError:
            pass
    return None


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
            os.execv(command[0], command)
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


class Attachment:
    """One WebSocket attached to a session, with its own output position."""

    def __init__(self, websocket, position: int) -> None:
        self.websocket = websocket
        self.position = position


class TerminalSession:
    def __init__(self, user: str, pid: int, fd: int, resumable: bool, on_closed: Callable[[], None]) -> None:
        self.user = user
        self.pid = pid
        self.fd = fd
        self.resumable = resumable
        self.secret = secrets.token_urlsafe(32) if resumable else None
        self.connected_at = datetime.now(timezone.utc).isoformat()
        self.client_ip: str | None = None
        self.control_path = None  # OpenSSH master socket that forks reuse.
        self.buffer = bytearray()
        self.base = 0  # Absolute output offset of buffer[0].
        self.received = 0  # Input bytes written to the PTY.
        self.exited = False
        self.ending = False
        self.attachment: Attachment | None = None
        self.changed = asyncio.Condition()
        self.finished = asyncio.Event()
        self._closed = False
        self._on_closed = on_closed
        self._expiry: asyncio.TimerHandle | None = None
        self._background: set[asyncio.Task] = set()
        self._pump = asyncio.create_task(self._pump_output())

    @property
    def end(self) -> int:
        return self.base + len(self.buffer)

    def status(self, session_id: str) -> dict:
        return {
            "id": session_id, "user": self.user, "connected_at": self.connected_at,
            "client_ip": self.client_ip, "attached": self.attachment is not None,
        }

    def _spawn(self, coroutine) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _notify(self) -> None:
        async with self.changed:
            self.changed.notify_all()

    def _trim(self) -> None:
        keep = OUTPUT_BUFFER
        if self.attachment is not None:
            keep = max(keep, self.end - self.attachment.position)
        excess = len(self.buffer) - keep
        if excess > 0:
            del self.buffer[:excess]
            self.base += excess

    async def _pump_output(self) -> None:
        # Keep reading while detached so OpenSSH never blocks on a full PTY.
        try:
            while True:
                try:
                    data = os.read(self.fd, READ_SIZE)
                except BlockingIOError:
                    await wait_for_fd(self.fd)
                    continue
                except OSError as exc:
                    if exc.errno == errno.EIO:  # Linux PTY slave closed.
                        return
                    raise
                if not data:
                    return
                async with self.changed:
                    self.buffer += data
                    self.changed.notify_all()
                    # An attached browser gets backpressure instead of gaps.
                    await self.changed.wait_for(
                        lambda: self.attachment is None or self.end - self.attachment.position <= OUTPUT_BUFFER
                    )
                    self._trim()
        finally:
            self.exited = True
            self._spawn(self._notify())

    async def _send_output(self, attachment: Attachment) -> None:
        websocket = attachment.websocket
        while True:
            async with self.changed:
                await self.changed.wait_for(lambda: attachment.position < self.end or self.exited)
                start = attachment.position - self.base
                chunk = bytes(self.buffer[start:start + READ_SIZE])
            if not chunk:
                await websocket.send(json.dumps({"type": "closed", "message": "SSH session ended"}))
                return
            await websocket.send(chunk)
            async with self.changed:
                attachment.position += len(chunk)
                self.changed.notify_all()

    async def _write_input(self, data: memoryview) -> None:
        while data:
            try:
                written = os.write(self.fd, data)
            except BlockingIOError:
                await wait_for_fd(self.fd, writable=True)
                continue
            except OSError as exc:
                if exc.errno != errno.EIO:
                    raise
                written = len(data)  # SSH exited; the bytes have nowhere to go.
            self.received += written
            data = data[written:]

    async def _receive_input(self, attachment: Attachment) -> None:
        websocket = attachment.websocket
        async for message in websocket:
            if self.attachment is not attachment:
                return  # A resumed connection owns the input count now.
            if not isinstance(message, str):
                await self._write_input(memoryview(message))
                continue
            control = control_message(message)
            kind = control.get("type")
            if kind == "resize":
                resize(self.fd, *dimensions(control))
            elif kind == "ping" and self.resumable:
                await websocket.send(json.dumps({"type": "pong", "in": self.received}))
            elif kind == "bye" and self.resumable:
                self.ending = True
                return
            else:
                raise ProtocolError("bad_request", "Expected a terminal resize message")

    def claim(self, websocket, position: int) -> Attachment:
        """Make this WebSocket the session's only attachment."""
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        previous = self.attachment
        self.attachment = Attachment(websocket, max(self.base, min(position, self.end)))
        self.connected_at = datetime.now(timezone.utc).isoformat()
        self.client_ip = client_ip(websocket)
        if previous is not None:
            # The old network path is usually dead; never wait on its close.
            self._spawn(previous.websocket.close(4000, "Resumed by another connection"))
        return self.attachment

    async def serve(self, attachment: Attachment, header: dict) -> None:
        websocket = attachment.websocket
        tasks: list[asyncio.Task] = []
        try:
            await websocket.send(json.dumps(header))
            await self._notify()
            tasks = [
                asyncio.create_task(self._send_output(attachment)),
                asyncio.create_task(self._receive_input(attachment)),
                asyncio.create_task(websocket.wait_closed()),
            ]
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
            if tasks[0] in done:
                self.ending = True
        except ConnectionClosed:
            raise
        except BaseException:
            self.ending = True
            raise
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if self.attachment is attachment:
                self.attachment = None
                await self._notify()
                if self.ending or not self.resumable:
                    await self.close()
                else:
                    loop = asyncio.get_running_loop()
                    self._expiry = loop.call_later(DETACH_TIMEOUT, lambda: self._spawn(self.close()))

    async def end_session(self, reason: str) -> None:
        self.ending = True
        attachment = self.attachment
        if attachment is not None:
            await attachment.websocket.close(1000, reason)
        else:
            await self.close()
        await self.finished.wait()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._expiry is not None:
            self._expiry.cancel()
        self._pump.cancel()
        await asyncio.gather(self._pump, return_exceptions=True)
        await stop_ssh(self.pid, self.fd)
        self._on_closed()
        self.finished.set()
