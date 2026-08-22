#!/usr/bin/env bash
# Deploy Archivum to perceo-control (VM 104 on jigserver).
#
# Run from a machine that can reach the Proxmox host. Every step is read-only
# until the explicit apply at the end, so you can run it with --check first and
# see exactly what would happen.
#
#   scripts/deploy-perceo-control.sh --check   # report state, change nothing
#   scripts/deploy-perceo-control.sh           # pull, rebuild, restart, verify
#
# Deliberately not run automatically: this restarts the stack that holds your
# vault, and a deploy you did not watch is a deploy you cannot roll back from
# quickly.

set -euo pipefail

PVE_HOST="${PVE_HOST:-jigserver}"
VMID="${ARCHIVUM_VMID:-104}"
APP_DIR="${ARCHIVUM_APP_DIR:-/opt/perceo/archivum}"
BRANCH="${ARCHIVUM_BRANCH:-main}"
CHECK_ONLY=0
[ "${1:-}" = "--check" ] && CHECK_ONLY=1

# `qm guest exec` returns JSON with the command's output nested inside it.
guest() {
  ssh "$PVE_HOST" "qm guest exec $VMID -- bash -lc $(printf '%q' "$1")" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); sys.stdout.write(d.get("out-data","")); sys.stderr.write(d.get("err-data","")); sys.exit(d.get("exitcode",0))'
}

echo "→ Proxmox host: $PVE_HOST, VM $VMID, app dir $APP_DIR, branch $BRANCH"

echo "→ Guest agent"
ssh "$PVE_HOST" "qm agent $VMID ping" >/dev/null && echo "  agent responding"

echo "→ Current state"
guest "cd $APP_DIR && git rev-parse --short HEAD && git status --porcelain | head"
guest "cd $APP_DIR && docker compose ps --format '{{.Service}} {{.Status}}'"

if [ "$CHECK_ONLY" = "1" ]; then
  echo "→ --check: stopping here, nothing changed."
  exit 0
fi

# Uncommitted changes on the server mean somebody edited in place. Stop rather
# than overwrite work that exists nowhere else.
if guest "cd $APP_DIR && git status --porcelain" | grep -q .; then
  echo "✗ The working tree on $VMID has uncommitted changes. Resolve those first." >&2
  exit 1
fi

echo "→ Pulling $BRANCH"
guest "cd $APP_DIR && git fetch --all --quiet && git checkout $BRANCH --quiet && git pull --ff-only"

# Deliberately not ./update.sh: that wrapper needs Node, which perceo-control
# does not have, so it exits before touching anything. Compose is what actually
# runs there.
echo "→ Rebuilding and restarting"
guest "cd $APP_DIR && docker compose build backend frontend mcp"
guest "cd $APP_DIR && docker compose up -d backend frontend mcp"

echo "→ Verifying"
guest "cd $APP_DIR && docker compose ps --format '{{.Service}} {{.Status}}'"
# Checked from inside the VM, not through the tunnel: scripted requests through
# Cloudflare trip the shared login rate limiter.
guest "curl -fsS -o /dev/null -w 'api %{http_code}\n' http://127.0.0.1:8000/api/system/health || echo 'api health check failed'"
guest "cd $APP_DIR && docker compose logs --tail 20 backend | tail -20"

echo "✓ Deployed. Sign the CLIs in if you have not yet:"
echo "    ssh $PVE_HOST \"qm guest exec $VMID -- docker exec -it archivum-backend claude login\""
echo "    ssh $PVE_HOST \"qm guest exec $VMID -- docker exec -it archivum-codex codex login\""
