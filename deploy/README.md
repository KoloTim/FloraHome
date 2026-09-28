# deploy/ — reproducible deployment helpers

These are the scripts used (and generalised) to stand the stack up on the demo
Pi. They are safe to read; they contain no secrets.

| File | Purpose |
|---|---|
| `install-docker-pi.sh` | Install Docker Engine + compose plugin on Debian/Raspberry Pi OS. |
| `deploy-stack.sh` | Unpack the tree, install `.env`, fix `data/` ownership, `compose up`, wait for health. |
| `remote_exec.py` | Run a shell script on a remote host over SSH non-interactively (password or key). Works from Windows. |

## Typical sequence

```bash
# 0. on the source host (devbox)
tar czf /tmp/smartplanter-src.tgz \
    --exclude='esphome/.esphome' --exclude='data' \
    --exclude='.env' --exclude='esphome/secrets.yaml' \
    -C /opt/stacks smartplanter
# copy smartplanter-src.tgz and a filled-in .env to the target as:
#   ~/smartplanter-src.tgz  and  ~/smartplanter.env

# 1. on the Pi
sudo bash deploy/install-docker-pi.sh        # then start a new SSH session
bash deploy/deploy-stack.sh

# 2. seed a demo chart
cd ~/smartplanter && ./scripts/seed_demo_data.sh
```

See `../DEPLOYMENT_HANDOFF.md` for the full narrative and traps.
