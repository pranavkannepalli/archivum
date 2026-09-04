import fs from "node:fs";
import path from "node:path";

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
  fs.mkdirSync(path.dirname(file), { recursive: true });
  const config = readJson(file);
  config.mcpServers = {
    ...(config.mcpServers ?? {}),
    archivum: { url: sseUrl, headers: { Authorization: `Bearer ${key}` } },
  };
  fs.writeFileSync(file, `${JSON.stringify(config, null, 2)}\n`);
  return file;
}

export function writeClaudeConfig({ home, sseUrl, key }) {
  return writeJsonServer(path.join(home, ".claude.json"), { sseUrl, key });
}

export function writeCursorConfig({ home, sseUrl, key }) {
  return writeJsonServer(path.join(home, ".cursor", "mcp.json"), { sseUrl, key });
}

export function writeCodexConfig({ home, sseUrl, key }) {
  const file = path.join(home, ".codex", "config.toml");
  fs.mkdirSync(path.dirname(file), { recursive: true });
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
  fs.writeFileSync(file, `${stripped.trimEnd()}\n${stripped.trim() ? "\n" : ""}${block}`.trimStart());
  return file;
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
