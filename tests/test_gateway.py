from __future__ import annotations

import asyncio
import json
import secrets
import socket
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from websockets.exceptions import ConnectionClosedError, InvalidStatusCode
from websockets.legacy.client import connect

from test_core import ROOT, load_module


gateway = load_module("quick_tunnel_gateway", ROOT / "src/cloudflared_quick_tunnel_gateway.py")


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.token = secrets.token_urlsafe(32)
        listening = asyncio.get_running_loop().create_future()
        print_patch = patch.object(gateway, "print", create=True, side_effect=lambda port, **kw: listening.set_result(port))
        identity_patch = patch.object(gateway, "stage_identity", return_value=None)
        self.addCleanup(print_patch.stop)
        self.addCleanup(identity_patch.stop)
        print_patch.start()
        identity_patch.start()
        self.control, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.control.setblocking(False)
        self.addCleanup(self.control.close)
        self.addCleanup(child.close)
        self.server_task = asyncio.create_task(gateway.run_gateway(self.token, Path(directory.name), child))
        self.addAsyncCleanup(self.stop_server)
        port = await asyncio.wait_for(listening, timeout=3)
        self.url = f"ws://127.0.0.1:{port}"

    async def stop_server(self) -> None:
        self.server_task.cancel()
        await asyncio.gather(self.server_task, return_exceptions=True)

    def hello(self, **overrides) -> str:
        hello = {"type": "hello", "v": 1, "user": "ubuntu", "token": self.token, "cols": 120, "rows": 35}
        return json.dumps(hello | overrides)

    async def request(self, action: str, **fields) -> dict:
        loop = asyncio.get_running_loop()
        await loop.sock_sendall(self.control, json.dumps({"action": action, **fields}).encode())
        return json.loads(await asyncio.wait_for(loop.sock_recv(self.control, 16 * 1024), timeout=4))

    async def test_wrong_token_and_invalid_parameters_never_start_ssh(self) -> None:
        with patch.object(gateway, "start_ssh") as start_ssh:
            cases = (
                (self.hello(token=secrets.token_urlsafe(32)), "unauthorized"),
                (self.hello(token="🍕"), "unauthorized"),
                (self.hello(user="-invalid"), "bad_request"),
                (self.hello(cols=True), "bad_request"),
                (self.hello(v=True), "bad_request"),
            )
            for message, code in cases:
                async with connect(self.url + "/ws", origin="https://static.example") as websocket:
                    await websocket.send(message)
                    response = json.loads(await websocket.recv())
                    self.assertEqual(response["type"], "error")
                    self.assertEqual(response["code"], code)
                    if code == "unauthorized":
                        self.assertEqual(response["message"], "Invalid terminal token")
                    with self.assertRaises(ConnectionClosedError) as closed:
                        await websocket.recv()
                    self.assertEqual(closed.exception.code, 1008)
            start_ssh.assert_not_called()
        self.assertEqual((await self.request("list"))["sessions"], [])

    async def test_only_exact_websocket_path_upgrades(self) -> None:
        for path in ("/", "/session", "/ws?query=ignored"):
            with self.assertRaises(InvalidStatusCode) as rejected:
                async with connect(self.url + path):
                    self.fail("Unexpected WebSocket upgrade")
            self.assertEqual(rejected.exception.status_code, 404)

    def fake_ssh(self, script: str, commands: list | None = None):
        """Run a local shell in place of OpenSSH, recording the requested command."""
        actual = gateway.start_ssh

        def spawn(command, cols, rows):
            if commands is not None:
                commands.append((command, cols, rows))
            return actual(["/bin/sh", "-c", script], cols, rows)

        return patch.object(gateway, "start_ssh", side_effect=spawn)

    async def read_bytes(self, websocket, size: int) -> bytes:
        received = bytearray()
        while len(received) < size:
            frame = await asyncio.wait_for(websocket.recv(), timeout=3)
            self.assertIsInstance(frame, bytes)
            received.extend(frame)
        return bytes(received)

    async def test_qtty_session_name_is_validated_before_ssh(self) -> None:
        commands: list = []
        with self.fake_ssh("exec cat", commands), patch.object(gateway.os, "access", return_value=True):
            for name, expected in (("agent-1", "ready"), ("x; rm -rf ~", "error"), ("", "error"), (7, "error")):
                async with connect(self.url + "/ws") as websocket:
                    await websocket.send(self.hello(attach=name))
                    self.assertEqual(json.loads(await websocket.recv())["type"], expected)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0][0][-3:], ["/usr/local/bin/qtty", "login", "agent-1"])

    async def test_valid_hello_targets_only_loopback_ssh(self) -> None:
        commands: list = []
        with self.fake_ssh("exec cat", commands), patch.object(gateway.os, "access", return_value=True):
            async with connect(self.url + "/ws", origin="https://static.example") as websocket:
                await websocket.send(self.hello(host="remote.example"))
                self.assertEqual(json.loads(await websocket.recv()), {"type": "ready", "v": 1})
        command, cols, rows = commands[0]
        self.assertEqual(command[0], "/usr/bin/ssh")
        self.assertEqual(command[-6:], ["-l", "ubuntu", "--", "127.0.0.1", "/usr/local/bin/qtty", "login"])
        self.assertIn("IdentityFile=none", command)
        self.assertIn("IdentityAgent=none", command)
        self.assertEqual((cols, rows), (120, 35))

    async def test_binary_pty_round_trip(self) -> None:
        payload = bytes(range(256)) * 513
        with self.fake_ssh("stty raw -echo && printf 'start' && exec cat"):
            async with connect(self.url + "/ws") as websocket:
                await websocket.send(self.hello())
                self.assertEqual(json.loads(await websocket.recv()), {"type": "ready", "v": 1})
                self.assertEqual(await self.read_bytes(websocket, 5), b"start")
                await websocket.send(payload)
                self.assertEqual(await self.read_bytes(websocket, len(payload)), payload)

    async def test_ending_one_registered_session_keeps_the_other_pty_open(self) -> None:
        async def open_terminal(websocket, user):
            await websocket.send(self.hello(user=user))
            self.assertEqual(json.loads(await websocket.recv()), {"type": "ready", "v": 1})
            self.assertEqual(await websocket.recv(), b"ready")

        with self.fake_ssh("stty raw -echo && printf 'ready' && exec cat"):
            async with connect(self.url + "/ws") as first, connect(self.url + "/ws") as second:
                await open_terminal(first, "first")
                await open_terminal(second, "second")
                registered = (await self.request("list"))["sessions"]
                self.assertEqual([item["user"] for item in registered], ["first", "second"])
                first_id = registered[0]["id"]
                result = await self.request("end", id=first_id)
                self.assertTrue(result["ended"])
                self.assertEqual(result["sessions"], registered[1:])
                self.assertEqual(first.close_code, 1000)
                self.assertFalse((await self.request("end", id=first_id))["ended"])
                await second.send(b"still alive")
                self.assertEqual(await second.recv(), b"still alive")

    async def test_legacy_session_closes_its_pty_with_the_socket(self) -> None:
        with self.fake_ssh("stty raw -echo && printf 'ready' && exec cat"):
            async with connect(self.url + "/ws") as websocket:
                await websocket.send(self.hello())
                await websocket.recv()
                self.assertEqual(await websocket.recv(), b"ready")
            for _ in range(40):
                if not (await self.request("list"))["sessions"]:
                    break
                await asyncio.sleep(0.05)
            self.assertEqual((await self.request("list"))["sessions"], [])

    async def open_resumable(self, script: str):
        websocket = await connect(self.url + "/ws")
        await websocket.send(self.hello(resume=True))
        ready = json.loads(await websocket.recv())
        self.assertEqual((ready["type"], ready["v"]), ("ready", 1))
        return websocket, ready

    def resume(self, ready: dict, offset: int, **overrides) -> str:
        request = {"type": "resume", "v": 1, "session": ready["session"], "resume": ready["resume"],
                   "offset": offset, "cols": 100, "rows": 30}
        return json.dumps(request | overrides)

    async def test_dropped_socket_resumes_with_missed_output_and_input_count(self) -> None:
        script = "stty raw -echo && printf 'ready' && exec cat"
        with self.fake_ssh(script):
            first, ready = await self.open_resumable(script)
            self.assertEqual(await self.read_bytes(first, 5), b"ready")
            await first.send(b"one")
            self.assertEqual(await self.read_bytes(first, 3), b"one")
            # Abort without a close frame, like a phone leaving Wi-Fi.
            first.transport.abort()
            await asyncio.sleep(0.2)
            sessions = (await self.request("list"))["sessions"]
            self.assertEqual(len(sessions), 1)
            self.assertFalse(sessions[0]["attached"])

            async with connect(self.url + "/ws") as second:
                await second.send(self.resume(ready, 5))
                resumed = json.loads(await second.recv())
                self.assertEqual(resumed, {"type": "resumed", "v": 1, "in": 3, "offset": 5})
                self.assertEqual(await self.read_bytes(second, 3), b"one")
                await second.send(b"two")
                self.assertEqual(await self.read_bytes(second, 3), b"two")
                await second.send(json.dumps({"type": "ping"}))
                self.assertEqual(json.loads(await second.recv()), {"type": "pong", "in": 6})
                self.assertTrue((await self.request("list"))["sessions"][0]["attached"])

    async def test_resume_takes_over_a_stale_connection(self) -> None:
        script = "stty raw -echo && printf 'ready' && exec cat"
        with self.fake_ssh(script):
            first, ready = await self.open_resumable(script)
            self.assertEqual(await self.read_bytes(first, 5), b"ready")
            async with connect(self.url + "/ws") as second:
                await second.send(self.resume(ready, 5))
                self.assertEqual(json.loads(await second.recv())["type"], "resumed")
                with self.assertRaises(ConnectionClosedError):
                    await asyncio.wait_for(first.recv(), timeout=3)
                self.assertEqual(first.close_code, 4000)
                await second.send(b"live")
                self.assertEqual(await self.read_bytes(second, 4), b"live")

    async def test_bye_ends_a_resumable_session(self) -> None:
        script = "stty raw -echo && printf 'ready' && exec cat"
        with self.fake_ssh(script):
            first, ready = await self.open_resumable(script)
            await first.send(json.dumps({"type": "bye"}))
            await first.close()
            for _ in range(40):
                if not (await self.request("list"))["sessions"]:
                    break
                await asyncio.sleep(0.05)
            async with connect(self.url + "/ws") as second:
                await second.send(self.resume(ready, 0))
                self.assertEqual(json.loads(await second.recv())["code"], "expired")

    async def test_fork_reuses_the_parent_login_and_requires_its_secret(self) -> None:
        commands: list = []
        script = "stty raw -echo && exec cat"
        with self.fake_ssh(script, commands), patch.object(gateway.os, "access", return_value=True):
            parent, ready = await self.open_resumable(script)
            self.addAsyncCleanup(parent.close)
            fork = {"type": "fork", "v": 1, "session": ready["session"], "resume": ready["resume"],
                    "cols": 90, "rows": 20, "fork": "main"}
            async with connect(self.url + "/ws") as websocket:
                await websocket.send(json.dumps(fork | {"resume": secrets.token_urlsafe(32)}))
                self.assertEqual(json.loads(await websocket.recv())["code"], "expired")
            async with connect(self.url + "/ws") as websocket:
                await websocket.send(json.dumps(fork))
                child = json.loads(await websocket.recv())
                self.assertEqual(child["type"], "ready")
                self.assertNotEqual(child["session"], ready["session"])
                await websocket.send(b"forked")
                self.assertEqual(await self.read_bytes(websocket, 6), b"forked")
        (parent_command, *_), (child_command, cols, rows) = commands
        control = next(item for item in parent_command if item.startswith("ControlPath="))
        self.assertIn(control, child_command)
        self.assertIn("ControlMaster=auto", child_command)
        self.assertEqual(child_command[-6:], ["--", "127.0.0.1", "/usr/local/bin/qtty", "login", "--fork", "main"])
        self.assertEqual((cols, rows), (90, 20))
        users = [item["user"] for item in (await self.request("list"))["sessions"]]
        self.assertEqual(users, ["ubuntu", "ubuntu"])

    async def test_resume_rejects_a_wrong_secret(self) -> None:
        script = "stty raw -echo && exec cat"
        with self.fake_ssh(script):
            first, ready = await self.open_resumable(script)
            self.addAsyncCleanup(first.close)
            for secret in (secrets.token_urlsafe(32), "🍕", None):
                async with connect(self.url + "/ws") as second:
                    await second.send(self.resume(ready, 0, resume=secret))
                    response = json.loads(await second.recv())
                    self.assertEqual(response["code"], "expired")
            self.assertTrue((await self.request("list"))["sessions"][0]["attached"])

    async def test_resumed_session_delivers_output_written_after_ssh_exits(self) -> None:
        with self.fake_ssh("stty raw -echo && read line && sleep 0.3 && printf 'bye'"):
            first, ready = await self.open_resumable("")
            await first.send(b"x\n")  # Raw mode has no CR-to-LF translation.
            await asyncio.sleep(0.05)
            first.transport.abort()
            await asyncio.sleep(0.6)  # SSH exits while no browser is attached.
            async with connect(self.url + "/ws") as second:
                await second.send(self.resume(ready, 0))
                self.assertEqual(json.loads(await second.recv())["type"], "resumed")
                received = bytearray()
                while True:
                    frame = await asyncio.wait_for(second.recv(), timeout=3)
                    if isinstance(frame, str):
                        self.assertEqual(json.loads(frame)["type"], "closed")
                        break
                    received.extend(frame)
                self.assertTrue(received.endswith(b"bye"))


class IdentityTests(unittest.TestCase):
    def test_openssh_requires_a_passphrase_for_the_staged_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, passphrase in (("unencrypted", ""), ("encrypted", "test-only-passphrase")):
                path = Path(tmp) / name
                subprocess.run(
                    ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path)], check=True,
                )
                self.assertEqual(gateway.encrypted_openssh_key(path.read_bytes()), bool(passphrase))
