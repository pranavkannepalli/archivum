import fs from "node:fs";
import path from "node:path";
import { spawnSync } from "node:child_process";

import { writeFileAtomic } from "./util.js";

const CODEX_BEGIN = "# >>> archivum >>>";
const CODEX_END = "# <<< archivum <<<";

function readJson(file) {
  if (!fs.existsSync(file)) return {};
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch {
    // A config we cannot parse is a config we must not overwrite silently.
    throw new Error(`${file} is not valid JSON. Fix or move it, then re-run.`);
  }
}

function writeJsonServer(file, { sseUrl, key }) {
  const config = readJson(file);
  config.mcpServers = {
    ...(config.mcpServers ?? {}),
    archivum: { url: sseUrl, headers: { Authorization: `Bearer ${key}` } },
  };
  return writeFileAtomic(file, `${JSON.stringify(config, null, 2)}\n`);
}

// `claude mcp add` is the supported way in, and it is the only one that is
// certain to write the file Claude Code actually loads. It also keeps us out of
// `~/.claude.json`, which is Claude Code's live state file — if the app is
// running (likely: the user just read a pairing token in it) our rewrite and
// its own would race, and one of the two would lose.
//
// `add` refuses a name that already exists, so a re-link would fail; remove
// first and ignore the result, which is also what makes this idempotent.
export function addClaudeServerViaCli({ sseUrl, key, spawnImpl = spawnSync }) {
  const run = (args) => spawnImpl("claude", args, { encoding: "utf8", stdio: "pipe" });
  const probe = run(["mcp", "list"]);
  // No `claude` on PATH (ENOENT) means the file writer is the only option.
  if (probe?.error || probe?.status !== 0) return null;
  run(["mcp", "remove", "archivum", "--scope", "user"]);
  const added = run([
    "mcp",
    "add",
    "--scope",
    "user",
    "--transport",
    "sse",
    "archivum",
    sseUrl,
    "--header",
    `Authorization: Bearer ${key}`,
  ]);
  if (added?.error || added?.status !== 0) return null;
  return "Claude Code (claude mcp add --scope user)";
}

export function writeClaudeConfig({ home, sseUrl, key, spawnImpl = spawnSync }) {
  return (
    addClaudeServerViaCli({ sseUrl, key, spawnImpl }) ??
    writeJsonServer(path.join(home, ".claude.json"), { sseUrl, key })
  );
}

export function writeCursorConfig({ home, sseUrl, key }) {
  return writeJsonServer(path.join(home, ".cursor", "mcp.json"), { sseUrl, key });
}

export function writeCodexConfig({ home, sseUrl, key }) {
  const file = path.join(home, ".codex", "config.toml");
  const existing = fs.existsSync(file) ? fs.readFileSync(file, "utf8") : "";
  // Fenced block rather than a TOML parse: the CLI has no dependencies, and
  // rewriting only what we own keeps hand-written settings untouched.
  const stripped = existing.replace(
    new RegExp(`${CODEX_BEGIN}[\\s\\S]*?${CODEX_END}\\n?`, "g"),
    "",
  );
  const block = [
    CODEX_BEGIN,
    "[mcp_servers.archivum]",
    `url = "${sseUrl}"`,
    `http_headers = { Authorization = "Bearer ${key}" }`,
    CODEX_END,
    "",
  ].join("\n");
  return writeFileAtomic(
    file,
    `${stripped.trimEnd()}\n${stripped.trim() ? "\n" : ""}${block}`.trimStart(),
  );
}

export const CLIENT_WRITERS = {
  claude: writeClaudeConfig,
  cursor: writeCursorConfig,
  codex: writeCodexConfig,
};

const MARKERS = {
  claude: [".claude.json", ".claude"],
  cursor: [".cursor"],
  codex: [".codex"],
};

export function detectClients(home) {
  return Object.entries(MARKERS)
    .filter(([, markers]) => markers.some((m) => fs.existsSync(path.join(home, m))))
    .map(([name]) => name);
}
