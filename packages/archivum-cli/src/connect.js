import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { parseOptions, writeFileAtomic } from "./util.js";
import { CLIENT_WRITERS, detectClients } from "./clients.js";

const STATE_DIR = ".archivum";
const STATE_FILE = "connection.json";

export function decodePairingToken(token) {
  if (typeof token !== "string" || !token.startsWith("arch1_")) {
    throw new Error("That is not an Archivum pairing token. Issue one from Settings.");
  }
  let payload;
  try {
    payload = JSON.parse(Buffer.from(token.slice("arch1_".length), "base64url").toString());
  } catch {
    throw new Error("Malformed pairing token.");
  }
  if (!payload.u || !payload.s) throw new Error("Malformed pairing token.");
  return { baseUrl: payload.u, secret: payload.s };
}

export async function redeem({ baseUrl, secret, deviceName, fetchImpl = fetch }) {
  const response = await fetchImpl(`${baseUrl}/api/mcp/pairing/redeem`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ secret, device_name: deviceName }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(body?.detail?.detail ?? `Pairing failed (HTTP ${response.status}).`);
  }
  return response.json();
}

// A 200 with a body missing what we're about to persist and hand to every
// client config is worse than a rejected request: the pairing token is
// already spent, and nothing downstream would notice until the agent tried
// to use a `Bearer undefined` header. Fail loudly before writing anything.
function assertRedeemResult(details) {
  const missing = ["device_id", "key", "sse_url"].filter((field) => !details?.[field]);
  if (missing.length > 0) {
    throw new Error(
      `Server response to pairing redeem was missing ${missing.join(", ")}. ` +
        "The pairing token has been spent; check the server before issuing another.",
    );
  }
}

// Checked before redeem spends the one-time token: an unsupported --client
// must not cost the user their only shot at that token.
function assertKnownClients(clients) {
  const unknown = clients.filter((client) => !CLIENT_WRITERS[client]);
  if (unknown.length > 0) {
    throw new Error(
      `Unknown client: ${unknown.join(", ")}. Supported: ${Object.keys(CLIENT_WRITERS).join(", ")}.`,
    );
  }
}

const LOOPBACK_HOSTS = new Set(["localhost", "127.0.0.1", "::1", "0.0.0.0"]);

function isLoopback(url) {
  try {
    return (
      LOOPBACK_HOSTS.has(new URL(url).hostname.toLowerCase().replace(/^\[|\]$/g, "")) ||
      new URL(url).hostname.toLowerCase().endsWith(".localhost")
    );
  } catch {
    return false;
  }
}

// `MCP_PUBLIC_URL` is optional on the server, and its fallback is
// `http://localhost:8001/sse` — which, written into this machine's client
// configs, names *this* machine's port 8001 rather than the vault. The server
// refuses to issue such a URL, but an older server, or one behind a proxy that
// rewrites it, still can; the failure otherwise surfaces days later as an
// unexplained MCP connection error with no thread back to pairing.
export function assertReachableSseUrl(baseUrl, sseUrl) {
  if (!isLoopback(sseUrl) || isLoopback(baseUrl)) return;
  throw new Error(
    `The server handed back an MCP endpoint of ${sseUrl}, but this vault is at ${baseUrl}. ` +
      "A loopback endpoint points at this machine, not at the vault, so no client config was written. " +
      "Set MCP_PUBLIC_URL on the server to the URL its MCP port is reachable at, restart the stack, and link again.",
  );
}

// The spec's step 4: do not report success for an endpoint nobody has spoken
// to. Every misconfiguration of the MCP URL — a proxy that fronts the API but
// not the MCP port, a wrong MCP_PUBLIC_URL path, a key the server does not
// know — otherwise produces a confident "Linked to ..." and a broken agent.
// A GET on the SSE endpoint with the device key is answered by the same
// bearer middleware every tool call goes through, so a 200 means this exact
// URL and this exact key work together.
export async function verifyConnection({ sseUrl, key, fetchImpl = fetch }) {
  let response;
  try {
    response = await fetchImpl(sseUrl, {
      headers: { Authorization: `Bearer ${key}`, Accept: "text/event-stream" },
      signal: AbortSignal.timeout(10_000),
    });
  } catch (error) {
    return {
      ok: false,
      reason: `could not reach ${sseUrl} (${error.message})`,
      hint: "Check that the MCP port is exposed through the same proxy as the API, and that MCP_PUBLIC_URL matches it.",
    };
  }
  // Headers are all we need; an SSE stream left open would hold the process.
  await Promise.resolve(response.body?.cancel?.()).catch(() => {});
  if (response.ok) return { ok: true };
  if (response.status === 401 || response.status === 403) {
    return {
      ok: false,
      reason: `${sseUrl} refused this device key (HTTP ${response.status})`,
      hint: "The key was minted seconds ago, so this usually means the URL belongs to a different server.",
    };
  }
  return {
    ok: false,
    reason: `${sseUrl} answered HTTP ${response.status}`,
    hint: "Check MCP_PUBLIC_URL on the server — it must include the /sse path and point at the MCP port.",
  };
}

export async function installSkill({ home, skillUrl, fetchImpl = fetch }) {
  const response = await fetchImpl(skillUrl).catch(() => null);
  // A server without a bundled skill is a working server; linking must not fail
  // over it. The tools are still there, the agent just gets no guidance on
  // which to reach for first.
  if (!response?.ok) return null;
  const target = path.join(home, ".claude", "skills", "archivum-memory", "SKILL.md");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, await response.text());
  return target;
}

function statePath(home) {
  return path.join(home, STATE_DIR, STATE_FILE);
}

export function saveState(home, state) {
  // Atomic, and 0600 every time: this file holds the raw device key, and a
  // re-link onto a file that already existed must not inherit its prior mode
  // or leave a truncated stub if the write dies halfway.
  return writeFileAtomic(statePath(home), `${JSON.stringify(state, null, 2)}\n`);
}

function readState(home) {
  const file = statePath(home);
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, "utf8")) : null;
}

export async function status(home, { fetchImpl = fetch } = {}) {
  const state = readState(home);
  if (!state) {
    console.log("Not linked. Run: archivum connect <pairing-token>");
    return;
  }
  console.log(`Linked to ${state.base_url} as ${state.device_id}`);
  console.log(`Clients configured: ${state.clients.join(", ") || "none"}`);
  // /devices/self authenticates with the device's own key (require_device on
  // the server), so a 200 here is a direct answer to "does this key still
  // work" rather than the owner-only /devices list, which 401s no matter
  // what the device key is.
  const response = await fetchImpl(`${state.base_url}/api/mcp/devices/self`, {
    headers: { Authorization: `Bearer ${state.key}` },
  }).catch(() => null);
  console.log(response?.ok ? "Key authenticates." : "Key no longer authenticates.");
}

export async function revoke(home, { fetchImpl = fetch } = {}) {
  const state = readState(home);
  if (!state) throw new Error("Nothing to revoke: this machine is not linked.");
  const response = await fetchImpl(`${state.base_url}/api/mcp/devices/self`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${state.key}` },
  }).catch(() => null);
  if (!response?.ok) {
    // An offline machine, or a key the server has already forgotten, must
    // never print "Revoked." while the local record — the only note of
    // which device_id to revoke from Settings — still exists to delete.
    throw new Error(
      `Could not confirm revocation with the server (device ${state.device_id}). ` +
        "Revoke it from Settings, or retry once the server is reachable. Local state was left in place.",
    );
  }
  fs.rmSync(statePath(home), { force: true });
  console.log("Revoked. Remove the archivum entry from your MCP clients if you want it gone from disk.");
}

// A re-link (a second machine's worth of clients, a fresh token after
// reinstalling an agent) used to overwrite the local record and leave the old
// device key live on the server forever, under a near-identical name, with
// nothing on this machine that remembers it. "Losing a laptop costs you one
// key" only holds if a machine has one key.
async function revokePreviousLink(previous, { fetchImpl }) {
  if (!previous?.key || !previous?.base_url) return null;
  const response = await fetchImpl(`${previous.base_url}/api/mcp/devices/self`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${previous.key}` },
  }).catch(() => null);
  return { deviceId: previous.device_id, revoked: Boolean(response?.ok) };
}

export async function connectCommand(
  args,
  { home = os.homedir(), fetchImpl = fetch, spawnImpl } = {},
) {
  const { flags, values, positionals } = parseOptions(args);

  if (flags.has("status")) return status(home, { fetchImpl });
  if (flags.has("revoke")) return revoke(home, { fetchImpl });

  const token = positionals[0];
  if (!token) {
    throw new Error("Usage: archivum connect <pairing-token> [--name NAME] [--client claude|cursor|codex]");
  }

  const { baseUrl, secret } = decodePairingToken(token);
  const requested = values.get("client");
  const clients = requested
    ? [].concat(requested)
    : detectClients(home);
  if (clients.length === 0) {
    throw new Error("No supported MCP client found. Install Claude Code, Cursor, or Codex, or pass --client.");
  }
  assertKnownClients(clients);

  const deviceName = values.get("name") ?? `${os.hostname()} / ${clients.join("+")}`;
  // Read before redeem, revoked after the new key is safely on disk: if redeem
  // fails, this machine keeps the key it already had.
  const previous = readState(home);
  const details = await redeem({ baseUrl, secret, deviceName, fetchImpl });
  assertRedeemResult(details);
  assertReachableSseUrl(baseUrl, details.sse_url);

  // Save state before running any client writer or fetching the skill: the
  // token is spent and the key is live on the server the instant redeem
  // returns, so from here on a partial failure (one writer throwing, a slow
  // skill fetch) must still leave the key recoverable rather than orphaned.
  saveState(home, {
    device_id: details.device_id,
    base_url: baseUrl,
    sse_url: details.sse_url,
    key: details.key,
    linked_at: new Date().toISOString(),
    clients,
  });

  const retired = await revokePreviousLink(previous, { fetchImpl });

  const written = [];
  for (const client of clients) {
    written.push(
      CLIENT_WRITERS[client]({ home, sseUrl: details.sse_url, key: details.key, spawnImpl }),
    );
  }

  const skillPath = details.skill_url
    ? await installSkill({ home, skillUrl: details.skill_url, fetchImpl })
    : null;

  const verification = await verifyConnection({
    sseUrl: details.sse_url,
    key: details.key,
    fetchImpl,
  });

  console.log(`Linked to ${details.vault_name ?? baseUrl} as "${deviceName}".`);
  for (const file of written) console.log(`  configured ${file}`);
  if (skillPath) console.log(`  installed ${skillPath}`);
  if (retired) {
    console.log(
      retired.revoked
        ? `  revoked the previous device key for this machine (${retired.deviceId})`
        : `  could not revoke the previous device key for this machine (${retired.deviceId}) — revoke it from Settings`,
    );
  }

  if (verification.ok) {
    console.log(`  verified ${details.sse_url} answers this device key`);
  } else {
    // Loud, and not fatal: the key is real and on disk, so telling the user to
    // start over would cost them a token for a problem re-linking cannot fix.
    console.log(`\nCould not verify the MCP endpoint: ${verification.reason}.`);
    console.log(`  ${verification.hint}`);
    console.log("  The configs above were written and the device key is valid;");
    console.log("  re-check with: archivum connect --status");
  }
  console.log("\nFor claude.ai or ChatGPT, add a custom connector:");
  console.log(`  URL:    ${details.sse_url}`);
  console.log(`  Header: Authorization: Bearer ${details.key}`);
  console.log("\nRestart your agent for the new server to appear.");
}
