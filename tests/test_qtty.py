from __future__ import annotations

import os
import pty
import shutil
import select
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_core import ROOT

QTTY = ROOT / "src/cloudflared_quick_tunnel_qtty.py"


class Terminal:
    """Runs qtty on its own PTY, the way SSH would."""

    def __init__(self, env: dict, *args: str) -> None:
        self.pid, self.fd = pty.fork()
        if self.pid == 0:
            os.execve(sys.executable, [sys.executable, "-I", str(QTTY), *args], env)
        self.output = bytearray()

    def send(self, data: bytes) -> None:
        os.write(self.fd, data)

    def expect(self, needle: bytes, timeout: float = 5) -> bytes:
        deadline = time.monotonic() + timeout
        while needle not in self.output:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError(f"missing {needle!r} in {bytes(self.output[-600:])!r}")
            if select.select([self.fd], [], [], remaining)[0]:
                try:
                    self.output += os.read(self.fd, 65536)
                except OSError:
                    break
        return bytes(self.output)

    def wait(self, timeout: float = 5) -> int:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid, status = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                try:
                    while select.select([self.fd], [], [], 0.05)[0]:
                        self.output += os.read(self.fd, 65536)
                except OSError:
                    pass
                os.close(self.fd)
                return os.waitstatus_to_exitcode(status)
            try:
                if select.select([self.fd], [], [], 0.05)[0]:
                    self.output += os.read(self.fd, 65536)
            except OSError:
                pass
        os.kill(self.pid, signal.SIGKILL)
        raise AssertionError("qtty did not exit")


class QttyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.mkdtemp(prefix="qtty-test-")
        os.chmod(directory, 0o700)
        self.env = {"QTTY_DIR": directory, "SHELL": "/bin/sh", "HOME": directory, "TERM": "xterm-256color",
                    "PATH": "/usr/bin:/bin", "PS1": "$ "}
        self.addCleanup(shutil.rmtree, directory, True)
        self.addCleanup(self.stop_holder)

    def qtty(self, *args: str) -> str:
        return subprocess.run([sys.executable, "-I", str(QTTY), *args], env=self.env,
                              capture_output=True, text=True, timeout=10).stdout

    def stop_holder(self) -> None:
        for line in self.qtty("ls").splitlines():
            name = line.split()[0]
            if name != "No":
                self.qtty("kill", name)
        time.sleep(0.3)

    def test_session_survives_detach_and_replays_output(self) -> None:
        first = Terminal(self.env, "new", "work")
        first.expect(b"\x1b]5379;qtty;work\x07")
        first.send(b"echo marker-$((6*7))\n")
        first.expect(b"marker-42")
        first.send(b"\x1d")  # Ctrl-]
        self.assertEqual(first.wait(), 0)
        self.assertIn(b"detached from work", first.output)
        listing = self.qtty("ls")
        self.assertIn("work", listing)
        self.assertIn("detached", listing)

        second = Terminal(self.env, "attach", "work")
        output = second.expect(b"marker-42")
        self.assertIn(b"\x1bc", output)  # The replay starts from a clean screen.
        second.send(b"echo again-$((5+5))\n")
        second.expect(b"again-10")
        second.send(b"exit\n")
        self.assertEqual(second.wait(), 0)
        self.assertIn(b"session ended: work", second.output)
        self.assertNotIn("work", self.qtty("ls"))

    def test_second_attach_takes_over(self) -> None:
        first = Terminal(self.env, "new", "shared")
        first.expect(b"qtty;shared")
        second = Terminal(self.env, "attach", "shared")
        second.expect(b"qtty;shared")
        self.assertEqual(first.wait(), 0)
        self.assertIn(b"attached elsewhere: shared", first.output)
        second.send(b"\x1d")
        second.wait()

    def test_full_screen_mode_is_restored_on_attach(self) -> None:
        first = Terminal(self.env, "new", "tui")
        first.expect(b"qtty;tui")
        # Enter the alternate screen and SGR mouse mode, then let the buffer move on.
        first.send(b"printf '\\033[?1049h\\033[?1000h\\033[?1006h'; echo ready\n")
        first.expect(b"ready")
        first.send(b"\x1d")
        first.wait()
        second = Terminal(self.env, "attach", "tui")
        output = second.expect(b"\x1b[?1006h")
        self.assertIn(b"\x1b[?1000h", output)
        second.send(b"\x1d")
        second.wait()

    def test_login_starts_a_session_when_none_exist_and_rejects_bad_names(self) -> None:
        login = Terminal(self.env, "login")
        login.expect(b"qtty;s1")
        login.send(b"\x1d")
        login.expect(b"choose a terminal session")
        login.expect(b"s1")
        login.send(b"q")
        self.assertEqual(login.wait(), 0)
        bad = subprocess.run([sys.executable, "-I", str(QTTY), "attach", "../x"], env=self.env,
                             capture_output=True, text=True, timeout=10)
        self.assertNotEqual(bad.returncode, 0)

    def test_fork_starts_a_new_session_in_the_parent_directory(self) -> None:
        work = Path(self.env["HOME"]) / "project"
        work.mkdir()
        parent = Terminal(self.env, "new", "main")
        parent.expect(b"qtty;main")
        parent.send(f"cd {work}\n".encode())
        time.sleep(0.3)
        parent.send(b"\x1d")
        parent.wait()
        fork = Terminal(self.env, "login", "--fork", "main")
        fork.expect(b"qtty;s1")
        fork.send(b"pwd\n")
        fork.expect(str(work).encode() + b"\r\n")
        fork.send(b"\x1d")
        fork.expect(b"choose a terminal session")
        fork.send(b"q")
        fork.wait()
        self.assertEqual(sorted(line.split()[0] for line in self.qtty("ls").splitlines()), ["main", "s1"])

    def test_nested_attach_is_refused(self) -> None:
        env = {**self.env, "QTTY_SESSION": "outer"}
        result = subprocess.run([sys.executable, "-I", str(QTTY), "new", "inner"], env=env,
                                capture_output=True, text=True, timeout=10)
        self.assertIn("already inside session outer", result.stderr)

    def test_private_directory_is_required(self) -> None:
        os.chmod(self.env["QTTY_DIR"], 0o755)
        result = subprocess.run([sys.executable, "-I", str(QTTY), "ls"], env=self.env,
                                capture_output=True, text=True, timeout=10)
        os.chmod(self.env["QTTY_DIR"], 0o700)
        self.assertIn("private directory", result.stderr)


if __name__ == "__main__":
    unittest.main()
