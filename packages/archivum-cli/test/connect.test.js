import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { spawnSync } from "node:child_process";

import { connectCommand, decodePairingToken, installSkill, redeem, revoke, saveState, status } from "../src/connect.js";

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
    connectCommand([token, "--client", "vscode"], { home, fetchImpl }),
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
    connectCommand([token, "--client", "claude"], { home, fetchImpl }),
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

  await assert.rejects(connectCommand([token, "--client", "claude"], { home, fetchImpl }));

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
