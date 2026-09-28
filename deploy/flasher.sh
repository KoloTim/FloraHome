#!/usr/bin/env bash
# FloraHome flasher service (runs inside esphome/esphome, as root, host USB).
#
# Responsibilities:
#   * expose the node configs and a discovery endpoint on 6053
#   * flash a node: esphome run esphome/<node>.yaml --device /dev/ttyUSBx
#
# Security: only reachable on the Pi's LAN and behind the dashboard login (the
# API calls it). Never flashed from the public internet.
set -uo pipefail

CONFIG=/config
PORT="${FLASHER_PORT:-6053}"

log() { echo "[flasher] $(date +%H:%M:%S) $*"; }

serial_devices() {
  ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null || true
}

# Minimal HTTP API (python is present in the esphome image).
python3 - "$PORT" <<'PY' &
import json, os, subprocess, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
CONFIG = "/config"

def devices():
    out = []
    for d in subprocess.run(["bash", "-lc", "ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null"],
                            capture_output=True, text=True).stdout.split():
        out.append({"port": d})
    return out

def nodes():
    return sorted(os.path.splitext(os.path.basename(p))[0]
                  for p in subprocess.run(["bash", "-lc", f"ls {CONFIG}/*.yaml 2>/dev/null"],
                                          capture_output=True, text=True).stdout.split()
                  if "secrets" not in p)

class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a): pass
    def do_GET(self):
        if self.path.startswith("/devices"):
            self._json(200, {"devices": devices()})
        elif self.path.startswith("/nodes"):
            self._json(200, {"nodes": nodes()})
        elif self.path.startswith("/health"):
            self._json(200, {"ok": True})
        else:
            self._json(404, {"error": "not found"})
    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad json"})
        if self.path.startswith("/flash"):
            node = body.get("node"); port = body.get("port", "/dev/ttyUSB0")
            if not node or not os.path.exists(f"{CONFIG}/{node}.yaml"):
                return self._json(400, {"error": "unknown node"})
            print(f"[flasher] flashing {node} on {port}", flush=True)
            p = subprocess.run(["esphome", "run", f"{CONFIG}/{node}.yaml",
                                "--device", port, "--no-logs"],
                               capture_output=True, text=True, timeout=900)
            return self._json(200 if p.returncode == 0 else 500,
                              {"ok": p.returncode == 0, "rc": p.returncode,
                               "tail": (p.stdout or p.stderr)[-2000:]})
        self._json(404, {"error": "not found"})

print(f"[flasher] HTTP on :{PORT}", flush=True)
HTTPServer(("0.0.0.0", PORT), H).serve_forever()
PY

log "watching for devices; configs in $CONFIG"
while true; do
  for d in $(serial_devices); do
    log "serial present: $d"
  done
  sleep 30
done
