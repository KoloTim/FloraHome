#!/usr/bin/env bash
# FloraHome host helper — small privileged HTTP shim for things a container
# cannot do: apply the Wi-Fi hotspot and flash an ESP over USB.
#
# Runs on the Pi host (systemd, user=root, but bound to localhost only). The
# API container reaches it via host.docker.internal:6054. Every call is
# authenticated with HOST_HELPER_TOKEN (shared with the API).
#
# Install: deploy/install-host-helper.sh
set -uo pipefail

PORT="${HOST_HELPER_PORT:-6054}"
TOKEN="${HOST_HELPER_TOKEN:-}"

log() { echo "[host-helper] $(date +%H:%M:%S) $*"; }

[ -n "$TOKEN" ] || { echo "HOST_HELPER_TOKEN not set; refusing to start"; exit 1; }

python3 - "$PORT" <<'PY'
import json, os, subprocess, sys
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1])
TOKEN = os.environ.get("HOST_HELPER_TOKEN", "")
ESP_HOME = os.environ.get("ESP_HOME", "/home/tim/smartplanter/esphome")


def run(cmd, timeout=900):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


class H(BaseHTTPRequestHandler):
    def _json(self, code, obj):
        body = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

    def _auth(self):
        return self.headers.get("X-Host-Token", "") == TOKEN

    def do_GET(self):
        if not self._auth():
            return self._json(401, {"error": "unauthorized"})
        if self.path.startswith("/health"):
            return self._json(200, {"ok": True, "ssid": self._ssid()})
        if self.path.startswith("/hotspot"):
            return self._json(200, self._hotspot())
        if self.path.startswith("/devices"):
            rc, out = run(["bash", "-lc", "for d in /dev/ttyUSB* /dev/ttyACM*; do "
                           "[ -e \"$d\" ] || continue; echo -n \"$d \"; "
                           "esptool --port \"$d\" flash_id 2>/dev/null | "
                           "grep -m1 MAC || echo; done"], timeout=60)
            devs = []
            for line in out.splitlines():
                parts = line.split()
                if parts:
                    devs.append({"port": parts[0], "mac": parts[1] if len(parts) > 1 else None})
            return self._json(200, {"devices": devs})
        self._json(404, {"error": "not found"})

    def do_POST(self):
        if not self._auth():
            return self._json(401, {"error": "unauthorized"})
        n = int(self.headers.get("Content-Length", 0))
        try:
            body = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._json(400, {"error": "bad json"})

        if self.path.startswith("/hotspot"):
            ssid = body.get("ssid"); psk = body.get("psk")
            cmds = []
            if ssid:
                cmds.append(["nmcli", "con", "mod", "Hotspot", "802-11-wireless.ssid", ssid])
            if psk:
                cmds.append(["nmcli", "con", "mod", "Hotspot", "802-11-wireless-security.psk", psk])
            if not cmds:
                return self._json(400, {"error": "nothing to change"})
            results = []
            for c in cmds:
                rc, out = run(c, timeout=30)
                results.append({"cmd": c[-2], "rc": rc, "out": out.strip()[:200]})
            run(["nmcli", "con", "down", "Hotspot"], 30)
            rc, out = run(["nmcli", "con", "up", "Hotspot"], 60)
            results.append({"cmd": "up Hotspot", "rc": rc, "out": out.strip()[:200]})
            return self._json(200, {"ok": all(r["rc"] == 0 for r in results), "results": results})

        if self.path.startswith("/flash"):
            node = body.get("node"); port = body.get("port", "/dev/ttyUSB0")
            cfg = f"{ESP_HOME}/{node}.yaml"
            if not node or not os.path.exists(cfg):
                return self._json(400, {"error": f"unknown node {node}"})
            print(f"[host-helper] flashing {node} on {port}", flush=True)
            rc, out = run(["esphome", "run", cfg, "--device", port, "--no-logs"], timeout=900)
            return self._json(200 if rc == 0 else 500,
                              {"ok": rc == 0, "rc": rc, "tail": out[-2000:]})
        self._json(404, {"error": "not found"})

    def _ssid(self):
        rc, out = run(["nmcli", "-g", "802-11-wireless.ssid", "con", "show", "Hotspot"], 20)
        return out.strip() if rc == 0 else None

    def _hotspot(self):
        return {"ssid": self._ssid(), "owner": "host"}


print(f"[host-helper] HTTP on :{PORT}", flush=True)
HTTPServer(("127.0.0.1", PORT), H).serve_forever()
PY
