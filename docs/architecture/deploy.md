# Deploying to perceo-control

Archivum runs on **perceo-control, VM 104 on jigserver**, reached through the
Proxmox guest agent.

```bash
scripts/deploy-perceo-control.sh --check   # report state, change nothing
scripts/deploy-perceo-control.sh           # pull, rebuild, restart, verify
```

The script is read-only until its final step, refuses to run if the working tree
on the VM has uncommitted changes, and verifies health from **inside** the VM
rather than through the tunnel — scripted requests through Cloudflare trip the
shared login rate limiter.

Overrides: `PVE_HOST`, `ARCHIVUM_VMID`, `ARCHIVUM_APP_DIR`, `ARCHIVUM_BRANCH`.

## After the first deploy of this change

Three things are inert until configured. None of them break anything by being
left alone; they simply do nothing.

**1. Sign the CLIs in**, so model work runs on your subscription rather than
per-token API calls:

```bash
docker exec -it archivum-backend claude login
docker exec -it archivum-backend codex login
```

Credentials live in the `codex_home` / `claude_home` volumes and survive
restarts. Then set both providers:

```
LLM_EXTRACTION_PROVIDER=claude_cli
LLM_SYNTHESIS_PROVIDER=claude_cli
```

Extraction is the heaviest model user — every ingested file goes through it —
so this is where the token spend actually is.

**2. Point session capture at your transcripts.** Nothing is captured until it
can see them, and the container cannot see your laptop:

```
TRANSCRIPT_HOST_DIR=/path/on/the/vm/to/transcripts
TRANSCRIPT_DIRS=/data/transcripts
```

**3. Expose MCP for chat clients** if you want claude.ai or ChatGPT to reach the
vault. It binds to `127.0.0.1:8001` by default, so it needs a proxy entry and a
non-empty `MCP_API_KEY` — bearer auth is only enforced when that is set. See
[agent access](./agent-access.md).

## Rolling back

```bash
cd /opt/perceo/archivum && git checkout <previous-sha>
docker compose build backend frontend mcp && docker compose up -d backend frontend mcp
```

Not `./update.sh`. That wrapper shells out to Node, which perceo-control does not
have, so it prints an install hint and exits without deploying anything. Compose
is what actually runs there. The app lives in `/opt/perceo/archivum`, alongside
the other Perceo services, not in `/opt/archivum`.

Git operations on the VM need root — the checkout is not owned by `kitts`, and
git refuses to touch a repository it sees as someone else's.
