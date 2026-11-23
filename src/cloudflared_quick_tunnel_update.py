#!/usr/bin/python3
"""Compare and install revisions from the project's GitHub main branch."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request, build_opener


REPOSITORY = "robit-man/cloudflared-quick-tunnel-tray"
COMMIT_URL = f"https://api.github.com/repos/{REPOSITORY}/commits/main"
ARCHIVE_URL = f"https://codeload.github.com/{REPOSITORY}/tar.gz"
INSTALLED_REVISION = Path("/usr/local/share/cloudflared-quick-tunnel/revision")
BROKER_SOCKET = "/run/cloudflared-quick-tunnel/control.sock"
CTL = "/usr/local/bin/cloudflared-quick-tunnelctl"
REVISION = re.compile(r"[0-9a-f]{40}")
ARCHIVE_LIMIT = 8 * 1024 * 1024
SOURCE_LIMIT = 32 * 1024 * 1024


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def download(url: str, maximum: int, accept: str) -> bytes:
    request = Request(url, headers={
        "Accept": accept,
        "User-Agent": "cloudflared-quick-tunnel-tray",
        "Cache-Control": "no-cache",
    })
    with build_opener(NoRedirect()).open(request, timeout=12) as response:
        payload = response.read(maximum + 1)
    if len(payload) > maximum:
        raise ValueError("GitHub response exceeds the download limit")
    return payload


def latest_revision() -> str:
    revision = download(COMMIT_URL, 128, "application/vnd.github.sha").decode("ascii").strip()
    if not REVISION.fullmatch(revision):
        raise ValueError("GitHub returned an invalid commit ID")
    return revision


def installed_revision() -> str | None:
    try:
        revision = INSTALLED_REVISION.read_text(encoding="ascii").strip()
    except (FileNotFoundError, UnicodeError):
        return None
    return revision if REVISION.fullmatch(revision) else None


def unpack_source(archive: bytes, revision: str, project: Path) -> None:
    archive_root = f"{REPOSITORY.rsplit('/', 1)[1]}-{revision}"
    remaining = SOURCE_LIMIT
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r|gz") as source:
        for member in source:
            root, *parts = member.name.split("/")
            if root != archive_root or any(part in {"", ".", ".."} for part in parts):
                raise ValueError("Unexpected path in the GitHub source archive")
            if not parts and member.isdir():
                continue
            destination = project.joinpath(*parts)
            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue
            if not parts or not member.isfile() or member.issparse() or not 0 <= member.size <= remaining:
                raise ValueError("Unsupported entry in the GitHub source archive")
            remaining -= member.size
            destination.parent.mkdir(parents=True, exist_ok=True)
            with source.extractfile(member) as incoming, destination.open("xb") as output:
                shutil.copyfileobj(incoming, output)
    if not (project / "install.sh").is_file():
        raise ValueError("The GitHub source archive is missing install.sh")
    (project / "REVISION").write_text(revision + "\n", encoding="ascii")


def install_latest() -> None:
    if os.geteuid() != 0:
        raise PermissionError("Installation requires administrator authorization")
    revision = latest_revision()
    if revision == installed_revision():
        print("Cloudflare Quick Tunnel is already up to date.", flush=True)
        return
    print(f"Installing Cloudflare Quick Tunnel revision {revision}.", flush=True)
    archive = download(f"{ARCHIVE_URL}/{revision}", ARCHIVE_LIMIT, "application/gzip")
    with tempfile.TemporaryDirectory(prefix="cloudflared-quick-tunnel-update-", dir="/run") as directory:
        project = Path(directory)
        unpack_source(archive, revision, project)
        subprocess.run(["/bin/sh", str(project / "install.sh")], cwd=project, check=True)


def tunnel_slots() -> list[dict]:
    """Every user's tunnel, from the broker. Empty if the broker is down."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(20)
            connection.connect(BROKER_SOCKET)
            connection.sendall(b'{"action":"slots"}\n')
            data = bytearray()
            while b"\n" not in data and len(data) < 256 * 1024:
                chunk = connection.recv(65536)
                if not chunk:
                    break
                data.extend(chunk)
        reply = json.loads(bytes(data).split(b"\n", 1)[0])
    except (OSError, ValueError):
        return []
    slots = reply.get("slots") if isinstance(reply, dict) and reply.get("ok") else None
    return [slot for slot in slots if isinstance(slot, dict)] if isinstance(slots, list) else []


def restore_command(slot: dict) -> list[str] | None:
    """The ctl command that starts SLOT's tunnel again as its owner."""
    user, mode, port = slot.get("owner_user"), slot.get("mode"), slot.get("port")
    if slot.get("state") not in ("starting", "ready") or not isinstance(user, str) or not user:
        return None
    runuser = shutil.which("runuser") or "/usr/sbin/runuser"
    if mode == "browser-tty":
        target = ["--browser"]
    elif mode == "native" and type(port) is int and 1 <= port <= 65535:
        target = ["--port", str(port)]
    else:
        return None
    return [runuser, "-u", user, "--", CTL, "start", *target]


def install_when_idle() -> None:
    """Install the latest revision unless a browser terminal is in use.

    Run by the automatic update timer. Installing restarts the broker, which
    ends tunnels and attached browser terminals, so a busy machine is left
    alone until the next check. Tunnels that were running come back in the
    same mode afterwards, with a new address.
    """
    revision = latest_revision()
    if revision == installed_revision():
        return
    slots = tunnel_slots()
    busy = sorted({slot.get("owner_user", "?") for slot in slots
                   if any(item.get("attached", True) for item in slot.get("browser_sessions") or [])})
    if busy:
        print(f"Update {revision[:8]} is waiting: a browser terminal is attached for {', '.join(busy)}.",
              flush=True)
        return
    restore = [command for command in map(restore_command, slots) if command]
    install_latest()
    for command in restore:
        # A failed restore leaves that tunnel stopped; the owner can start it from the tray.
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, timeout=90, check=False)
        state = "restored" if result.returncode == 0 else f"not restored ({result.stderr.decode(errors='replace').strip()[:200]})"
        print(f"Tunnel for {command[2]} {state}.", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="check the public GitHub main branch")
    mode.add_argument("--install", action="store_true", help="install the latest revision as root")
    mode.add_argument("--auto", action="store_true",
                      help="install the latest revision as root when no browser terminal is attached")
    args = parser.parse_args()
    try:
        if args.check:
            print(json.dumps({"installed": installed_revision(), "latest": latest_revision()}))
        elif args.auto:
            install_when_idle()
        else:
            install_latest()
    except (OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as exc:
        print(f"Quick Tunnel update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
