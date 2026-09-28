#!/usr/bin/env bash
# FloraHome — add / build / flash a plant node.
#
# A node config is a copy of esphome/smartplanter.yaml with its own
# `device_name` (the MQTT node id) and `friendly_name`. Everything else
# (pins, calibration, safety) is shared, so a new node is one command.
#
# Usage (run from the repo root, or anywhere — paths are resolved):
#   deploy/add-node.sh new  <device> "<friendly>"     create esphome/<device>.yaml
#   deploy/add-node.sh list                            list nodes
#   deploy/add-node.sh discover                        show attached ESPs (esptool flash_id)
#   deploy/add-node.sh compile <device>                build firmware
#   deploy/add-node.sh flash   <device> [port]         USB flash (first time)
#   deploy/add-node.sh ota     <device> <ip>           over-the-air update
#
# device: [a-z0-9-]+  e.g. plant-d
# NOTE: node files live in esphome/ itself (not a subdir) so ESPHome finds
#       esphome/secrets.yaml for the !secret references.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
ESP="$REPO/esphome"
TEMPLATE="$ESP/smartplanter.yaml"
BATTERY_TEMPLATE="$ESP/battery-template.yaml"
NODES="$ESP"

ESPHOME="${ESPHOME:-$(command -v esphome || echo "$HOME/.local/bin/esphome")}"
ESPTOOL="${ESPTOOL:-$(command -v esptool || echo "python3 -m esptool")}"

die() { echo "error: $*" >&2; exit 1; }

case "${1:-}" in
  new)
    dev="${2:?usage: add-node.sh new <device> \"<friendly>\" [battery]}"
    friendly="${3:-$dev}"
    kind="${4:-}"
    [[ "$dev" =~ ^[a-z0-9-]+$ ]] || die "device must be [a-z0-9-]+"
    mkdir -p "$NODES"
    [ -f "$NODES/$dev.yaml" ] && die "$NODES/$dev.yaml already exists"
    if [ "$kind" = "battery" ]; then
      src="$BATTERY_TEMPLATE"
      [ -f "$src" ] || die "missing $src"
    else
      src="$TEMPLATE"
    fi
    sed -e "s|^  device_name: .*|  device_name: $dev|" \
        -e "s|^  friendly_name: .*|  friendly_name: \"$friendly\"|" \
        "$src" > "$NODES/$dev.yaml"
    echo "created $NODES/$dev.yaml (device_name=$dev, friendly=$friendly, kind=${kind:-pump})"
    echo "next: edit it if needed, then: deploy/add-node.sh compile $dev"
    ;;

  list)
    ls -1 "$NODES"/*.yaml 2>/dev/null | xargs -n1 basename | sed 's/\.yaml$//' \
      | grep -v '^smartplanter$' || true
    ;;

  discover)
    shopt -s nullglob
    found=0
    for d in /dev/ttyUSB* /dev/ttyACM*; do
      found=1
      echo "== $d =="
      $ESPTOOL --port "$d" flash_id 2>&1 | grep -iE 'Chip is|MAC:|Detected flash size' || echo "  (no response)"
    done
    [ "$found" = 0 ] && echo "no serial devices found (/dev/ttyUSB* /dev/ttyACM*)"
    ;;

  compile)
    dev="${2:?usage: add-node.sh compile <device>}"
    [ -f "$NODES/$dev.yaml" ] || die "no such node: $dev"
    "$ESPHOME" compile "$NODES/$dev.yaml"
    ;;

  flash)
    dev="${2:?usage: add-node.sh flash <device> [port]}"
    port="${3:-/dev/ttyUSB0}"
    [ -f "$NODES/$dev.yaml" ] || die "no such node: $dev"
    echo "Flashing $dev on $port ..."
    "$ESPHOME" run "$NODES/$dev.yaml" --device "$port" --no-logs
    ;;

  ota)
    dev="${2:?usage: add-node.sh ota <device> <ip>}"
    ip="${3:?usage: add-node.sh ota <device> <ip>}"
    [ -f "$NODES/$dev.yaml" ] || die "no such node: $dev"
    "$ESPHOME" run "$NODES/$dev.yaml" --device "$ip" --no-logs
    ;;

  *)
    sed -n '2,20p' "$0"; exit 1;;
esac
