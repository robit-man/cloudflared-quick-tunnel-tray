#!/usr/bin/python3
"""Compare and install revisions from the project's GitHub main branch."""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="check the public GitHub main branch")
    mode.add_argument("--install", action="store_true", help="install the latest revision as root")
    args = parser.parse_args()
    try:
        if args.check:
            print(json.dumps({"installed": installed_revision(), "latest": latest_revision()}))
        else:
            install_latest()
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as exc:
        print(f"Quick Tunnel update failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
