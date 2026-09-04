import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";

import { detectClients, writeClaudeConfig, writeCursorConfig, writeCodexConfig } from "../src/clients.js";

function tempHome() {
  return fs.mkdtempSync(path.join(os.tmpdir(), "archivum-home-"));
}

const opts = (home) => ({ home, sseUrl: "https://vault.example.com/sse", key: "amk_test" });

test("detectClients finds only the clients that are installed", () => {
  const home = tempHome();
  fs.mkdirSync(path.join(home, ".cursor"), { recursive: true });

  assert.deepEqual(detectClients(home), ["cursor"]);
});

test("writeClaudeConfig adds an archivum server with the bearer header", () => {
  const home = tempHome();

  const written = writeClaudeConfig(opts(home));
  const config = JSON.parse(fs.readFileSync(written, "utf8"));

  assert.equal(config.mcpServers.archivum.url, "https://vault.example.com/sse");
  assert.equal(config.mcpServers.archivum.headers.Authorization, "Bearer amk_test");
});

test("writeClaudeConfig preserves other servers and is idempotent", () => {
  const home = tempHome();
  const target = path.join(home, ".claude.json");
  fs.writeFileSync(target, JSON.stringify({ mcpServers: { other: { url: "http://x" } } }));

  writeClaudeConfig(opts(home));
  writeClaudeConfig(opts(home));
  const config = JSON.parse(fs.readFileSync(target, "utf8"));

  assert.equal(config.mcpServers.other.url, "http://x");
  assert.equal(Object.keys(config.mcpServers).length, 2);
});

test("writeCursorConfig writes to .cursor/mcp.json", () => {
  const home = tempHome();

  const written = writeCursorConfig(opts(home));

  assert.equal(written, path.join(home, ".cursor", "mcp.json"));
  assert.match(fs.readFileSync(written, "utf8"), /amk_test/);
});

test("writeCodexConfig writes a toml block and replaces it on re-run", () => {
  const home = tempHome();

  writeCodexConfig(opts(home));
  const written = writeCodexConfig(opts(home));
  const toml = fs.readFileSync(written, "utf8");

  assert.equal(written, path.join(home, ".codex", "config.toml"));
  assert.equal(toml.match(/\[mcp_servers\.archivum\]/g).length, 1);
  assert.match(toml, /url = "https:\/\/vault\.example\.com\/sse"/);
});

test("writeCodexConfig leaves unrelated toml intact", () => {
  const home = tempHome();
  const target = path.join(home, ".codex", "config.toml");
  fs.mkdirSync(path.dirname(target), { recursive: true });
  fs.writeFileSync(target, 'model = "gpt-5"\n');

  writeCodexConfig(opts(home));

  assert.match(fs.readFileSync(target, "utf8"), /model = "gpt-5"/);
});
