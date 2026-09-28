#!/usr/bin/env python3
"""
remote_exec.py - run a shell script on a remote host over SSH, non-interactively,
from Windows or any OS. Handy when the remote needs a password (no sshpass on
Windows) and you want output captured cleanly.

Usage:
    # password auth
    RHOST=192.168.91.68 RUSER=tim RPW=secret RSCRIPT=./cmd.sh python remote_exec.py
    # key auth
    RHOST=host RUSER=user RKEY=~/.ssh/id_ed25519 RSCRIPT=./cmd.sh python remote_exec.py
    # inline command
    RHOST=host RUSER=user RPW=secret python remote_exec.py 'echo hi; uname -a'

Notes:
  * The script is base64-encoded and piped to `bash` on the remote, so quoting
    and multi-line scripts survive intact.
  * Retries the connection a few times (some hosts rate-limit/behave oddly).
  * Requires: pip install paramiko
"""
import os
import sys
import time
import base64

import paramiko

host = os.environ["RHOST"]
user = os.environ["RUSER"]
pw = os.environ.get("RPW") or None
key = os.environ.get("RKEY") or None
timeout = int(os.environ.get("RTIMEOUT", "300"))

script_file = os.environ.get("RSCRIPT")
if script_file:
    with open(script_file, "rb") as fh:
        raw = fh.read().decode()
elif len(sys.argv) > 1:
    raw = sys.argv[1]
else:
    raw = sys.stdin.read()

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

remote = "echo %s | base64 -d | bash" % base64.b64encode(raw.encode()).decode()

last = None
for attempt in range(6):
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    kw = dict(username=user, timeout=15, banner_timeout=25, auth_timeout=25,
              allow_agent=False, look_for_keys=False)
    if key:
        kw["key_filename"] = os.path.expanduser(key)
    else:
        kw["password"] = pw
    try:
        client.connect(host, **kw)
    except Exception as exc:  # noqa: BLE001
        last = exc
        time.sleep(4 + attempt * 2)
        continue

    _stdin, stdout, stderr = client.exec_command(remote, timeout=timeout)
    out = stdout.read().decode(errors="replace")
    err = stderr.read().decode(errors="replace")
    rc = stdout.channel.recv_exit_status()
    sys.stdout.write(out)
    if err:
        sys.stderr.write(err)
    print("__RC__=%d" % rc)
    client.close()
    sys.exit(0)

print("CONNECT_FAILED: %r" % (last,))
sys.exit(2)
