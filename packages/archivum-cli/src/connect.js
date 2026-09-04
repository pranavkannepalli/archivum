import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { parseOptions } from "./util.js";
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
  const file = statePath(home);
  fs.mkdirSync(path.dirname(file), { recursive: true });
  fs.writeFileSync(file, `${JSON.stringify(state, null, 2)}\n`, { mode: 0o600 });
  // `mode` on writeFileSync only applies when the file is created; a re-link
  // onto a file that already existed would otherwise keep its prior mode.
  // This file holds the raw device key, so tighten it every time.
  fs.chmodSync(file, 0o600);
  return file;
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

export async function connectCommand(args, { home = os.homedir(), fetchImpl = fetch } = {}) {
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
  const details = await redeem({ baseUrl, secret, deviceName, fetchImpl });
  assertRedeemResult(details);

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

  const written = [];
  for (const client of clients) {
    written.push(CLIENT_WRITERS[client]({ home, sseUrl: details.sse_url, key: details.key }));
  }

  const skillPath = details.skill_url
    ? await installSkill({ home, skillUrl: details.skill_url, fetchImpl })
    : null;

  console.log(`Linked to ${details.vault_name ?? baseUrl} as "${deviceName}".`);
  for (const file of written) console.log(`  configured ${file}`);
  if (skillPath) console.log(`  installed ${skillPath}`);
  console.log("\nFor claude.ai or ChatGPT, add a custom connector:");
  console.log(`  URL:    ${details.sse_url}`);
  console.log(`  Header: Authorization: Bearer ${details.key}`);
  console.log("\nRestart your agent for the new server to appear.");
}
