#!/usr/bin/env bash
set -euo pipefail

asset=$1
destination=$2
version=2026.11.2
expected=$(awk -v name="$asset" '$2 == name {print $1}' packaging/cloudflared.sha256)
test -n "$expected"
mkdir -p "$destination"
gh release download "$version" --repo cloudflare/cloudflared --pattern "$asset" --dir "$destination" --clobber
if command -v sha256sum >/dev/null 2>&1; then
    printf '%s  %s\n' "$expected" "$destination/$asset" | sha256sum -c -
else
    printf '%s  %s\n' "$expected" "$destination/$asset" | shasum -a 256 -c -
fi
gh api -H 'Accept: application/vnd.github.raw+json' \
    "repos/cloudflare/cloudflared/contents/LICENSE?ref=$version" > "$destination/cloudflared-LICENSE"
