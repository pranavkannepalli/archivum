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

Four things are inert until configured. None of them break anything by being
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

On a hosted deployment this only covers transcripts on the VM. Agents running on
your laptops write their transcripts to those laptops, which the server cannot
read, so **automatic capture does not cover them**. What works today is explicit
`record_work` — it is an MCP tool, so it travels over the wire from any linked
machine, and the `archivum-memory` skill is what gets an agent to call it without
being asked. A transcript shipper that closes this gap is planned; until it ships,
do not assume work done on a laptop appears in the stream on its own.

**3. Set the public URLs** so pairing hands out addresses that work off the VM:

```
API_PUBLIC_URL=https://archivum.example.com
MCP_PUBLIC_URL=https://archivum-mcp.example.com/sse
```

Pairing tokens embed `API_PUBLIC_URL`; without it the base URL is derived from the
request uvicorn sees, whose scheme can differ from what the client used through
the tunnel, and a token can carry `http://` for an `https://` server. Redeeming a
token hands back `MCP_PUBLIC_URL` as the SSE endpoint. Without it the only URL
available is `http://localhost:8001/sse`, which is right on the VM and useless
anywhere else — so redeem refuses with HTTP 503 and `mcp_url_unresolved` rather
than configuring a laptop to talk to its own port 8001. The refusal does not
spend the token: set `MCP_PUBLIC_URL`, restart the stack, and run `connect` again
with the same one.

**4. Link your machines and expose MCP.** MCP binds to `127.0.0.1:8001`, so a
proxy entry is what makes it reachable at all. Once it is published, issue a
pairing token from Settings → Agent Access and run
`archivum connect <token>` on each machine you code from. Each gets its own
revocable key; `MCP_API_KEY` is no longer required, and HTTP now refuses
unauthenticated requests whether or not it is set. See
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
