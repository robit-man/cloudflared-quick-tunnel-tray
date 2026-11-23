from __future__ import annotations

import io
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from test_core import ROOT, load_module


update = load_module("quick_tunnel_update", ROOT / "src/cloudflared_quick_tunnel_update.py")
REVISION = "2a" * 20
ARCHIVE_ROOT = f"cloudflared-quick-tunnel-tray-{REVISION}"


def make_archive(*files: tuple[tarfile.TarInfo, bytes]) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for entry, content in files:
            entry.size = len(content)
            archive.addfile(entry, io.BytesIO(content))
    return buffer.getvalue()


class UpdateTests(unittest.TestCase):
    def test_main_commit_must_be_a_full_sha(self) -> None:
        with patch.object(update, "download", return_value=REVISION.encode()) as download:
            self.assertEqual(update.latest_revision(), REVISION)
            download.assert_called_once_with(update.COMMIT_URL, 128, "application/vnd.github.sha")
        for invalid in (b"main", b"../../file", b"1" * 41):
            with self.subTest(invalid=invalid), patch.object(update, "download", return_value=invalid):
                with self.assertRaisesRegex(ValueError, "invalid commit"):
                    update.latest_revision()

    def test_archive_extracts_and_records_the_exact_revision(self) -> None:
        archive = make_archive(
            (tarfile.TarInfo(ARCHIVE_ROOT + "/install.sh"), b"exit 0\n"),
            (tarfile.TarInfo(ARCHIVE_ROOT + "/src/indicator.py"), b"pass\n"),
        )
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            update.unpack_source(archive, REVISION, project)
            self.assertEqual((project / "REVISION").read_text(), REVISION + "\n")
            self.assertEqual((project / "src/indicator.py").read_bytes(), b"pass\n")

    def test_archive_rejects_external_paths_links_and_different_commits(self) -> None:
        symlink = tarfile.TarInfo(ARCHIVE_ROOT + "/install.sh")
        symlink.type = tarfile.SYMTYPE
        symlink.linkname = "/etc/passwd"
        for entry in (
            tarfile.TarInfo(ARCHIVE_ROOT + "/../outside"),
            tarfile.TarInfo("/etc/passwd"),
            tarfile.TarInfo("wrong-commit/install.sh"),
            symlink,
        ):
            with self.subTest(entry=entry.name), tempfile.TemporaryDirectory() as directory:
                with self.assertRaises(ValueError):
                    update.unpack_source(make_archive((entry, b"")), REVISION, Path(directory))

    def test_install_requires_root_before_network_access(self) -> None:
        with patch.object(update.os, "geteuid", return_value=1000), patch.object(update, "latest_revision") as latest:
            with self.assertRaises(PermissionError):
                update.install_latest()
            latest.assert_not_called()

    def test_current_version_does_not_download_or_restart(self) -> None:
        with (
            patch.object(update.os, "geteuid", return_value=0),
            patch.object(update, "latest_revision", return_value=REVISION),
            patch.object(update, "installed_revision", return_value=REVISION),
            patch.object(update, "download") as download,
            patch.object(update.subprocess, "run") as run,
            patch("builtins.print"),
        ):
            update.install_latest()
            download.assert_not_called()
            run.assert_not_called()


INSTALLED = "1b" * 20


def slot(user, state="ready", mode="browser-tty", port=22, sessions=()):
    return {"owner_user": user, "state": state, "mode": mode, "port": port, "browser_sessions": list(sessions)}


class AutomaticUpdateTests(unittest.TestCase):
    def run_auto(self, slots, latest=REVISION):
        with (
            patch.object(update, "latest_revision", return_value=latest),
            patch.object(update, "installed_revision", return_value=INSTALLED),
            patch.object(update, "tunnel_slots", return_value=slots),
            patch.object(update, "install_latest") as install,
            patch.object(update.subprocess, "run") as run,
            patch.object(update.shutil, "which", return_value="/usr/sbin/runuser"),
            patch("builtins.print"),
        ):
            run.return_value.returncode = 0
            update.install_when_idle()
        return install, run

    def test_current_revision_does_nothing(self) -> None:
        install, run = self.run_auto([slot("roko")], latest=INSTALLED)
        install.assert_not_called()
        run.assert_not_called()

    def test_attached_browser_terminal_defers_the_update(self) -> None:
        busy = slot("roko", sessions=[{"id": "a" * 32, "attached": True}])
        install, run = self.run_auto([busy, slot("other")])
        install.assert_not_called()
        run.assert_not_called()

    def test_detached_sessions_do_not_block_and_tunnels_come_back(self) -> None:
        detached = slot("roko", sessions=[{"id": "a" * 32, "attached": False}])
        native = slot("dev", mode="native", port=8080)
        stopped = slot("idle", state="stopped")
        install, run = self.run_auto([detached, native, stopped])
        install.assert_called_once_with()
        commands = [call.args[0] for call in run.call_args_list]
        ctl = "/usr/local/bin/cloudflared-quick-tunnelctl"
        self.assertEqual(commands, [
            ["/usr/sbin/runuser", "-u", "roko", "--", ctl, "start", "--browser"],
            ["/usr/sbin/runuser", "-u", "dev", "--", ctl, "start", "--port", "8080"],
        ])

    def test_restore_ignores_malformed_slots(self) -> None:
        for bad in (slot("", ), slot(None), slot("roko", mode="other"), slot("roko", mode="native", port=True),
                    slot("roko", mode="native", port=70000)):
            with self.subTest(slot=bad):
                self.assertIsNone(update.restore_command(bad))
