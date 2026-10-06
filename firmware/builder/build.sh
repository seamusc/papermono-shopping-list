#!/usr/bin/env bash
# Build the firmware in Docker; PlatformIO and its toolchains are never installed on the host.
#   build.sh [-c config.env] [-o out-dir]
# Per-install settings are injected at build time, never stored in the image or the source tree:
#   SHOPPING_LIST_WIFI_SSID, SHOPPING_LIST_WIFI_PASSWORD, SHOPPING_LIST_SERVER_URL (http://host, no trailing slash)
# taken from the environment, or from -c FILE (KEY=VALUE lines, no quotes). All three are always required --
# entrypoint.sh fails the build with a clear message if any is unset; there is no fallback to a placeholder
# (secrets.example.h) or to an existing ../src/secrets.h.
# Output: <out-dir>/<FW_VERSION>.bin (default ../dist, which is git-ignored).
# The first run downloads ~2 GB of toolchains into the `papermono-pio` Docker volume; later runs reuse it.
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fw="$(dirname "$here")"
config="" out="$fw/dist"
while getopts "c:o:" o; do
  case "$o" in c) config="$(readlink -f "$OPTARG")";; o) out="$(readlink -m "$OPTARG")";; *) exit 2;; esac
done
[ -z "$config" ] || [ -f "$config" ] || { echo "no such config file: $config" >&2; exit 1; }
mkdir -p "$out"

docker build -q -t papermono-firmware-builder "$here" >/dev/null
args=(--rm --user "$(id -u):$(id -g)" -v papermono-pio:/cache -v "$fw":/src:ro -v "$out":/out)
[ -z "$config" ] || args+=(--env-file "$config")
# Pass through from the caller's environment when set (a value from -c is not overridden by an unset variable).
for v in SHOPPING_LIST_WIFI_SSID SHOPPING_LIST_WIFI_PASSWORD SHOPPING_LIST_SERVER_URL; do
  [ -z "${!v:-}" ] || args+=(-e "$v")
done
docker run "${args[@]}" papermono-firmware-builder
