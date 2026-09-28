#!/usr/bin/env bash
# Compiles an ESP32 node firmware inside the official ESPHome container.
# Run BEFORE driving to the venue: it catches C++/lambda errors that
# `esphome config` cannot see.
#
# Usage: scripts/build_firmware.sh [esphome/<device>.yaml]
#        (default: esphome/plant-a.yaml)
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="$(pwd)"
ESPHOME_DIR="$REPO/esphome"
NODE="${1:-esphome/plant-a.yaml}"
[ -f "$NODE" ] || { echo "ERROR: no such node config: $NODE"; exit 1; }
# Config paths are passed relative to the mounted /config (esphome/) dir.
REL="${NODE#esphome/}"

[ -f "$ESPHOME_DIR/secrets.yaml" ] || {
  echo "ERROR: esphome/secrets.yaml missing."
  echo "       cp esphome/secrets.yaml.example esphome/secrets.yaml  and fill it in."
  exit 1
}

echo "==> Validating config ($REL)"
docker run --rm -v "$ESPHOME_DIR":/config esphome/esphome:2025.8 config "/config/$REL" >/dev/null
echo "    config OK"

echo "==> Compiling (first run downloads the toolchain, allow 5-15 min)"
docker run --rm -v "$ESPHOME_DIR":/config esphome/esphome:2025.8 compile "/config/$REL"

BUILD="$(basename "${REL%.yaml}")"
echo
echo "==> Firmware built for $BUILD. To flash:"
echo "    deploy/add-node.sh flash $BUILD                  # over USB (first time)"
echo "    deploy/add-node.sh ota   $BUILD <node-ip>        # over the air"
