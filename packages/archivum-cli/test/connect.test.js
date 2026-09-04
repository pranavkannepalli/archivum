import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { spawnSync } from "node:child_process";

import {
  assertReachableSseUrl,
  connectCommand,
  decodePairingToken,
  installSkill,
  redeem,
  revoke,
  saveState,
  status,
  verifyConnection,
} from "../src/connect.js";

// connectCommand hands this to the Claude writer. Without it the writer would
// shell out to the developer's real `claude` binary and rewrite their own MCP
// config; "not installed" routes the test through the file writer instead.
const noClaudeCli = () => ({ error: Object.assign(new Error("spawn claude ENOENT"), { code: "ENOENT" }) });

function encode(baseUrl, secret) {
  const payload = Buffer.from(JSON.stringify({ u: baseUrl, s: secret })).toString("base64url");
  return `arch1_${payload}`;
}

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "archivum-home-"));
}

test("decodePairingToken recovers the base url and secret", () => {
  const decoded = decodePairingToken(encode("https://vault.example.com", "s3cr3t"));

  assert.equal(decoded.baseUrl, "https://vault.example.com");
  assert.equal(decoded.secret, "s3cr3t");
});

test("decodePairingToken rejects a token that is not ours", () => {
  assert.throws(() => decodePairingToken("hunter2"), /pairing token/i);
});

test("redeem posts the secret and returns the device details", async () => {
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, body: JSON.parse(init.body) });
    return {
      ok: true,
      json: async () => ({ device_id: "dev_1", key: "amk_1", sse_url: "https://v/sse" }),
    };
  };

  const result = await redeem({
    baseUrl: "https://vault.example.com",
    secret: "s3cr3t",
    deviceName: "laptop",
    fetchImpl,
  });

  assert.equal(calls[0].url, "https://vault.example.com/api/mcp/pairing/redeem");
  assert.deepEqual(calls[0].body, { secret: "s3cr3t", device_name: "laptop" });
  assert.equal(result.key, "amk_1");
});

test("redeem surfaces the server's refusal message", async () => {
  const fetchImpl = async () => ({
    ok: false,
    status: 400,
    json: async () => ({ detail: { detail: "Pairing token is not valid.", code: "pairing_refused" } }),
  });

  await assert.rejects(
    redeem({ baseUrl: "https://v", secret: "x", deviceName: "l", fetchImpl }),
    /Pairing token is not valid/,
  );
});

test("installSkill writes the skill the server served", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-skill-"));
  const fetchImpl = async () => ({ ok: true, text: async () => "# Archivum Memory\n" });

  const written = await installSkill({ home, skillUrl: "https://v/api/mcp/skill", fetchImpl });

  assert.equal(written, path.join(home, ".claude", "skills", "archivum-memory", "SKILL.md"));
  assert.equal(fs.readFileSync(written, "utf8"), "# Archivum Memory\n");
});

test("installSkill overwrites a stale copy so the skill can be updated", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-skill-"));
  const target = path.join(home, ".claude", "skills", "archivum-memory", "SKILL.md");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, "# old\n");
  const fetchImpl = async () => ({ ok: true, text: async () => "# new\n" });

  await installSkill({ home, skillUrl: "https://v/api/mcp/skill", fetchImpl });

  assert.equal(fs.readFileSync(target, "utf8"), "# new\n");
});

test("installSkill returns null when the server bundles no skill", async () => {
  const home = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-skill-"));
  const fetchImpl = async () => ({ ok: false, status: 404 });

  assert.equal(await installSkill({ home, skillUrl: "https://v/api/mcp/skill", fetchImpl }), null);
});

test("saveState writes the connection file with mode 0o600 and tightens pre-existing files", () => {
  const home = tempHome();
  const target = path.join(home, ".archivum", "connection.json");
  const state = {
    device_id: "dev_1",
    base_url: "https://vault.example.com",
    sse_url: "https://vault.example.com/sse",
    key: "amk_1",
    linked_at: "2026-09-03T00:00:00.000Z",
    clients: ["claude"],
  };

  // Fresh file should have mode 0o600
  saveState(home, state);
  let mode = fs.statSync(target).mode & 0o777;
  assert.equal(mode, 0o600);

  // Pre-existing file at 0o644 should be tightened to 0o600
  fs.chmodSync(target, 0o644);
  assert.equal(fs.statSync(target).mode & 0o777, 0o644);
  saveState(home, state);
  mode = fs.statSync(target).mode & 0o777;
  assert.equal(mode, 0o600);
});

function linkedState(overrides = {}) {
  return {
    device_id: "dev_1",
    base_url: "https://vault.example.com",
    sse_url: "https://vault.example.com/sse",
    key: "amk_1",
    linked_at: "2026-09-03T00:00:00.000Z",
    clients: ["claude"],
    ...overrides,
  };
}

test("status calls the self endpoint with the stored key and reports it authenticates", async (t) => {
  const home = tempHome();
  saveState(home, linkedState());
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, headers: init.headers });
    return { ok: true };
  };

  await status(home, { fetchImpl });

  assert.equal(calls[0].url, "https://vault.example.com/api/mcp/devices/self");
  assert.equal(calls[0].headers.Authorization, "Bearer amk_1");
  assert.ok(logs.some((line) => /Key authenticates\./.test(line)));
});

test("status reports the key no longer authenticates when the self endpoint refuses it", async (t) => {
  const home = tempHome();
  saveState(home, linkedState());
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const fetchImpl = async () => ({ ok: false, status: 401 });

  await status(home, { fetchImpl });

  assert.ok(logs.some((line) => /Key no longer authenticates\./.test(line)));
});

test("status reports the key no longer authenticates when the server is unreachable", async (t) => {
  const home = tempHome();
  saveState(home, linkedState());
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const fetchImpl = async () => {
    throw new Error("ECONNREFUSED");
  };

  await status(home, { fetchImpl });

  assert.ok(logs.some((line) => /Key no longer authenticates\./.test(line)));
});

test("revoke deletes local state only once the server confirms the self-revoke", async (t) => {
  const home = tempHome();
  saveState(home, linkedState());
  t.mock.method(console, "log", () => {});
  const calls = [];
  const fetchImpl = async (url, init) => {
    calls.push({ url, method: init.method, headers: init.headers });
    return { ok: true, json: async () => ({ revoked: true }) };
  };

  await revoke(home, { fetchImpl });

  assert.equal(calls[0].url, "https://vault.example.com/api/mcp/devices/self");
  assert.equal(calls[0].method, "DELETE");
  assert.equal(calls[0].headers.Authorization, "Bearer amk_1");
  assert.equal(fs.existsSync(path.join(home, ".archivum", "connection.json")), false);
});

test("revoke refuses to delete local state when the server does not confirm", async () => {
  const home = tempHome();
  saveState(home, linkedState());
  const fetchImpl = async () => ({ ok: false, status: 401 });

  await assert.rejects(revoke(home, { fetchImpl }), /dev_1|Settings/);

  assert.equal(fs.existsSync(path.join(home, ".archivum", "connection.json")), true);
});

test("revoke refuses to delete local state when the server is unreachable", async () => {
  const home = tempHome();
  saveState(home, linkedState());
  const fetchImpl = async () => {
    throw new Error("ECONNREFUSED");
  };

  await assert.rejects(revoke(home, { fetchImpl }), /Settings|reachable/);

  assert.equal(fs.existsSync(path.join(home, ".archivum", "connection.json")), true);
});

test("connectCommand rejects an unknown --client before the pairing token is redeemed", async () => {
  const home = tempHome();
  let fetchCalled = false;
  const fetchImpl = async () => {
    fetchCalled = true;
    throw new Error("must not be called: client validation should happen first");
  };
  const token = encode("https://vault.example.com", "s3cr3t");

  await assert.rejects(
    connectCommand([token, "--client", "vscode"], { home, fetchImpl, spawnImpl: noClaudeCli }),
    /Unknown client: vscode/,
  );

  assert.equal(fetchCalled, false);
  assert.equal(fs.existsSync(path.join(home, ".archivum", "connection.json")), false);
});

test("connectCommand refuses to write any client config when the redeem response is missing required fields", async () => {
  const home = tempHome();
  const fetchImpl = async (url) => {
    assert.equal(url, "https://vault.example.com/api/mcp/pairing/redeem");
    return { ok: true, json: async () => ({ device_id: "dev_1" }) }; // missing key, sse_url
  };
  const token = encode("https://vault.example.com", "s3cr3t");

  await assert.rejects(
    connectCommand([token, "--client", "claude"], { home, fetchImpl, spawnImpl: noClaudeCli }),
    /key, sse_url|missing/,
  );

  assert.equal(fs.existsSync(path.join(home, ".claude.json")), false);
  assert.equal(fs.existsSync(path.join(home, ".archivum", "connection.json")), false);
});

test("connectCommand saves state before running client writers, so a writer failure still leaves the key recoverable", async () => {
  const home = tempHome();
  // A pre-existing .claude.json that is not valid JSON makes writeClaudeConfig
  // throw (see clients.js's readJson) — simulating a mid-loop writer failure.
  fs.writeFileSync(path.join(home, ".claude.json"), "{not valid json");
  const fetchImpl = async (url) => {
    if (url.endsWith("/pairing/redeem")) {
      return {
        ok: true,
        json: async () => ({ device_id: "dev_1", key: "amk_1", sse_url: "https://vault.example.com/sse" }),
      };
    }
    return { ok: false, status: 404 };
  };
  const token = encode("https://vault.example.com", "s3cr3t");

  await assert.rejects(connectCommand([token, "--client", "claude"], { home, fetchImpl, spawnImpl: noClaudeCli }));

  const state = JSON.parse(fs.readFileSync(path.join(home, ".archivum", "connection.json"), "utf8"));
  assert.equal(state.device_id, "dev_1");
  assert.equal(state.key, "amk_1");
});

test("connect runs without a repo checkout or an env file", () => {
  const cwd = fs.mkdtempSync(path.join(os.tmpdir(), "archivum-nowhere-"));

  const result = spawnSync("node", [path.resolve("src/index.js"), "connect"], {
    cwd,
    encoding: "utf8",
    env: { ...process.env, PATH: process.env.PATH },
  });

  // No token given, so it must fail on usage — never on a missing .env or root.
  assert.match(result.stderr, /Usage: archivum connect/);
  assert.doesNotMatch(result.stderr, /install directory|repository root|\.env/i);
});


const REDEEMED = {
  device_id: "dev_2",
  key: "amk_2",
  sse_url: "https://vault.example.com/sse",
  vault_name: "pranav",
};

// A fetchImpl that answers redeem, the skill fetch, the SSE verification and
// the self-revoke, recording every call so tests can assert on the sequence.
function scriptedFetch({ sse = { ok: true }, redeemBody = REDEEMED, onDelete = () => ({ ok: true }) } = {}) {
  const calls = [];
  const fetchImpl = async (url, init = {}) => {
    calls.push({ url, method: init.method ?? "GET", headers: init.headers });
    if (url.endsWith("/pairing/redeem")) return { ok: true, json: async () => redeemBody };
    if (url.endsWith("/api/mcp/devices/self")) return onDelete();
    if (url === redeemBody.sse_url) {
      if (typeof sse === "function") return sse();
      return sse;
    }
    return { ok: false, status: 404 };
  };
  return { calls, fetchImpl };
}

test("verifyConnection reports success when the SSE endpoint accepts the device key", async () => {
  const seen = [];
  const fetchImpl = async (url, init) => {
    seen.push({ url, auth: init.headers.Authorization, accept: init.headers.Accept });
    return { ok: true, status: 200, body: { cancel: async () => {} } };
  };

  const result = await verifyConnection({ sseUrl: "https://v/sse", key: "amk_1", fetchImpl });

  assert.deepEqual(result, { ok: true });
  assert.equal(seen[0].url, "https://v/sse");
  assert.equal(seen[0].auth, "Bearer amk_1");
  assert.equal(seen[0].accept, "text/event-stream");
});

test("verifyConnection reports a refused key rather than a generic failure", async () => {
  const fetchImpl = async () => ({ ok: false, status: 401 });

  const result = await verifyConnection({ sseUrl: "https://v/sse", key: "amk_1", fetchImpl });

  assert.equal(result.ok, false);
  assert.match(result.reason, /refused this device key \(HTTP 401\)/);
  assert.ok(result.hint);
});

test("verifyConnection reports an unreachable endpoint with the URL it tried", async () => {
  const fetchImpl = async () => {
    throw new Error("ECONNREFUSED");
  };

  const result = await verifyConnection({ sseUrl: "https://v/sse", key: "amk_1", fetchImpl });

  assert.equal(result.ok, false);
  assert.match(result.reason, /could not reach https:\/\/v\/sse/);
  assert.match(result.reason, /ECONNREFUSED/);
});

test("verifyConnection reports the status for a wrong path", async () => {
  const fetchImpl = async () => ({ ok: false, status: 404 });

  const result = await verifyConnection({ sseUrl: "https://v/sse", key: "amk_1", fetchImpl });

  assert.equal(result.ok, false);
  assert.match(result.reason, /HTTP 404/);
});

test("connectCommand verifies the endpoint it just wrote and says so", async (t) => {
  const home = tempHome();
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const { calls, fetchImpl } = scriptedFetch();

  await connectCommand([encode("https://vault.example.com", "s"), "--client", "claude"], {
    home,
    fetchImpl,
    spawnImpl: noClaudeCli,
  });

  const verify = calls.find((call) => call.url === "https://vault.example.com/sse");
  assert.equal(verify.headers.Authorization, "Bearer amk_2");
  assert.ok(logs.some((line) => /verified https:\/\/vault\.example\.com\/sse/.test(line)));
});

test("connectCommand says the endpoint is unverified instead of claiming success", async (t) => {
  const home = tempHome();
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const { fetchImpl } = scriptedFetch({
    sse: () => {
      throw new Error("ECONNREFUSED");
    },
  });

  await connectCommand([encode("https://vault.example.com", "s"), "--client", "claude"], {
    home,
    fetchImpl,
    spawnImpl: noClaudeCli,
  });

  const output = logs.join("\n");
  assert.match(output, /Could not verify the MCP endpoint/);
  assert.match(output, /ECONNREFUSED/);
  assert.doesNotMatch(output, /verified https/);
  // Non-fatal: the config and the key are still on disk.
  assert.equal(fs.existsSync(path.join(home, ".claude.json")), true);
  assert.equal(fs.existsSync(path.join(home, ".archivum", "connection.json")), true);
});

test("connectCommand refuses a loopback SSE url for a remote vault before writing config", async () => {
  const home = tempHome();
  const { fetchImpl } = scriptedFetch({
    redeemBody: { ...REDEEMED, sse_url: "http://localhost:8001/sse" },
  });

  await assert.rejects(
    connectCommand([encode("https://vault.example.com", "s"), "--client", "claude"], {
      home,
      fetchImpl,
      spawnImpl: noClaudeCli,
    }),
    /MCP_PUBLIC_URL/,
  );

  assert.equal(fs.existsSync(path.join(home, ".claude.json")), false);
});

test("assertReachableSseUrl allows a localhost vault and a localhost endpoint", () => {
  assertReachableSseUrl("http://localhost:8000", "http://localhost:8001/sse");
  assertReachableSseUrl("https://vault.example.com", "https://mcp.example.com/sse");
});

test("re-linking revokes the device key this machine held before", async (t) => {
  const home = tempHome();
  saveState(home, linkedState({ device_id: "dev_old", key: "amk_old" }));
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const { calls, fetchImpl } = scriptedFetch();

  await connectCommand([encode("https://vault.example.com", "s"), "--client", "claude"], {
    home,
    fetchImpl,
    spawnImpl: noClaudeCli,
  });

  const deletes = calls.filter((call) => call.method === "DELETE");
  assert.equal(deletes.length, 1);
  assert.equal(deletes[0].headers.Authorization, "Bearer amk_old");
  assert.ok(logs.some((line) => /revoked the previous device key.*dev_old/.test(line)));
  // The new key replaced the old one in the local record.
  const state = JSON.parse(fs.readFileSync(path.join(home, ".archivum", "connection.json"), "utf8"));
  assert.equal(state.key, "amk_2");
});

test("a previous key that cannot be revoked names its device id instead of aborting the link", async (t) => {
  const home = tempHome();
  saveState(home, linkedState({ device_id: "dev_old", key: "amk_old" }));
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const { fetchImpl } = scriptedFetch({
    onDelete: () => {
      throw new Error("ECONNREFUSED");
    },
  });

  await connectCommand([encode("https://vault.example.com", "s"), "--client", "claude"], {
    home,
    fetchImpl,
    spawnImpl: noClaudeCli,
  });

  assert.ok(logs.some((line) => /could not revoke the previous device key.*\(dev_old\)/.test(line)));
  assert.ok(logs.some((line) => /revoke it from Settings/.test(line)));
  const state = JSON.parse(fs.readFileSync(path.join(home, ".archivum", "connection.json"), "utf8"));
  assert.equal(state.key, "amk_2");
});

test("a first link revokes nothing and says nothing about a previous key", async (t) => {
  const home = tempHome();
  const logs = [];
  t.mock.method(console, "log", (msg) => logs.push(msg));
  const { calls, fetchImpl } = scriptedFetch();

  await connectCommand([encode("https://vault.example.com", "s"), "--client", "claude"], {
    home,
    fetchImpl,
    spawnImpl: noClaudeCli,
  });

  assert.equal(calls.filter((call) => call.method === "DELETE").length, 0);
  assert.ok(!logs.some((line) => /previous device key/.test(line)));
});
