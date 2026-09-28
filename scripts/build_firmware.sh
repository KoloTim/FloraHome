#!/usr/bin/env bash
# Compiles the ESP32 firmware inside the official ESPHome container.
# Run this BEFORE driving to the venue: it downloads the toolchain and catches
# C++/lambda errors that `esphome config` cannot see.
set -euo pipefail
cd "$(dirname "$0")/.."

ESPHOME_DIR="$(pwd)/esphome"
[ -f "$ESPHOME_DIR/secrets.yaml" ] || {
  echo "ERROR: esphome/secrets.yaml missing."
  echo "       cp esphome/secrets.yaml.example esphome/secrets.yaml  and fill it in."
  exit 1
}

echo "==> Validating config"
docker run --rm -v "$ESPHOME_DIR":/config esphome/esphome:2025.8 config /config/smartplanter.yaml >/dev/null
echo "    config OK"

echo "==> Compiling (first run downloads the toolchain, allow 5-15 min)"
docker run --rm -v "$ESPHOME_DIR":/config esphome/esphome:2025.8 compile /config/smartplanter.yaml

echo
echo "==> Firmware built. To flash:"
echo "    cd esphome && esphome upload smartplanter.yaml          # over USB"
echo "    cd esphome && esphome upload smartplanter.yaml --device <node-ip>   # OTA"
