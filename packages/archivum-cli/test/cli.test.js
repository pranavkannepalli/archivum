import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";

test("help lists command groups", () => {
  const result = spawnSync("node", ["src/index.js", "--help"], {
    cwd: new URL("..", import.meta.url),
    encoding: "utf8",
  });

  assert.equal(result.status, 0);
  assert.match(result.stdout, /Usage: archivum <command>/);
  assert.match(result.stdout, /install/);
  assert.match(result.stdout, /update/);
  assert.match(result.stdout, /uninstall/);
  assert.match(result.stdout, /stack/);
  assert.match(result.stdout, /mcp/);
  assert.match(result.stdout, /connect/);
  assert.match(result.stdout, /wiki/);
});
