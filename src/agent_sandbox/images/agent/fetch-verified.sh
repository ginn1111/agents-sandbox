#!/bin/sh
# Download to a temporary directory; publish nothing unless the pinned digest matches.
set -eu
url=$1 digest=$2 dest=$3
case "$url" in https://*) ;; *) exit 2 ;; esac
case "$digest" in ''|*[!0-9a-f]*) exit 2 ;; esac
[ "${#digest}" -eq 64 ] || exit 2
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' \
    --retry 3 --connect-timeout 20 --max-time 240 -o "$work/asset" "$url"
(cd "$work" && printf '%s  asset\n' "$digest" | sha256sum --check --strict)
mv "$work/asset" "$dest"
