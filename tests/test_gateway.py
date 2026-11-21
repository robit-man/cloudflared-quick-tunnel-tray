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

    async def test_valid_hello_targets_only_loopback_ssh(self) -> None:
        async def acknowledge(websocket, *_args):
            await websocket.send(json.dumps({"type": "ready", "v": 1}))

        with patch.object(gateway, "relay", side_effect=acknowledge) as relay:
            async with connect(self.url + "/ws", origin="https://static.example") as websocket:
                await websocket.send(self.hello(host="remote.example"))
                self.assertEqual(json.loads(await websocket.recv()), {"type": "ready", "v": 1})
            _websocket, command, cols, rows = relay.call_args.args
            self.assertEqual(command[-4:], ["-l", "ubuntu", "--", "127.0.0.1"])
            self.assertIn("IdentityFile=none", command)
            self.assertIn("IdentityAgent=none", command)
            self.assertEqual((cols, rows), (120, 35))

    async def test_binary_pty_round_trip(self) -> None:
        actual_relay = gateway.relay

        async def echo(websocket, _command, cols, rows):
            command = ["/bin/sh", "-c", "stty raw -echo && printf 'start' && exec cat"]
            await actual_relay(websocket, command, cols, rows)

        payload = bytes(range(256)) * 513
        with patch.object(gateway, "relay", side_effect=echo), patch.object(gateway, "SSH", "/bin/sh"):
            async with connect(self.url + "/ws") as websocket:
                await websocket.send(self.hello())
                self.assertEqual(json.loads(await websocket.recv()), {"type": "ready", "v": 1})
                marker = bytearray()
                while len(marker) < 5:
                    marker.extend(await asyncio.wait_for(websocket.recv(), timeout=3))
                self.assertEqual(marker, b"start")
                await websocket.send(payload)
                received = bytearray()
                while len(received) < len(payload):
                    received.extend(await asyncio.wait_for(websocket.recv(), timeout=3))
                self.assertEqual(received, payload)

    async def test_ending_one_registered_session_keeps_the_other_pty_open(self) -> None:
        actual_relay = gateway.relay

        async def echo(websocket, _command, cols, rows):
            await actual_relay(websocket, ["/bin/sh", "-c", "stty raw -echo && printf 'ready' && exec cat"], cols, rows)

        async def open_terminal(websocket, user):
            await websocket.send(self.hello(user=user))
            self.assertEqual(json.loads(await websocket.recv()), {"type": "ready", "v": 1})
            self.assertEqual(await websocket.recv(), b"ready")

        with patch.object(gateway, "relay", side_effect=echo), patch.object(gateway, "SSH", "/bin/sh"):
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


class IdentityTests(unittest.TestCase):
    def test_openssh_requires_a_passphrase_for_the_staged_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for name, passphrase in (("unencrypted", ""), ("encrypted", "test-only-passphrase")):
                path = Path(tmp) / name
                subprocess.run(
                    ["ssh-keygen", "-q", "-t", "ed25519", "-N", passphrase, "-f", str(path)], check=True,
                )
                self.assertEqual(gateway.encrypted_openssh_key(path.read_bytes()), bool(passphrase))
