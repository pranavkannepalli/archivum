import fs from "node:fs";
import path from "node:path";
import readline from "node:readline/promises";
import { stdin as input, stdout as output } from "node:process";

export function printHelp() {
  console.log(`Usage: archivum <command> [options]

Commands:
  install [--images|--build] [--yes] [--set KEY=VALUE]
  update [--images|--build]
  recovery <backup|validate|restore> [backup-dir] [--dir PATH] [--yes]
  uninstall [--volumes] [--images] [--files] [--yes] [--dry-run]
  stack <up|down|restart|logs|ps|build|shell>
  config <get|set|doctor>
  mcp config [--client claude|cursor|sse]
  connect <pairing-token> [--name NAME] [--client claude|cursor|codex]
  wiki <ingest|search|query|pages|open|write|lint|graph|rebuild-indexes>

Run from an Archivum install directory or repository root.`);
}

// Every file this CLI writes on a linked machine is a live file someone else
// owns: `~/.claude.json` is Claude Code's running state (project history,
// onboarding, caches), `~/.codex/config.toml` is hand-edited, and
// `~/.archivum/connection.json` is the only record of the device key. A plain
// writeFileSync truncates before it writes, so a Ctrl-C or a full disk leaves
// the user with an unparseable file and no copy of what was there. Write beside
// the target and rename: rename is atomic within a directory, so a reader sees
// either the old file or the new one.
export function writeFileAtomic(file, contents, { mode = 0o600 } = {}) {
  const dir = path.dirname(file);
  fs.mkdirSync(dir, { recursive: true });
  const tmp = path.join(dir, `.${path.basename(file)}.${process.pid}.tmp`);
  try {
    fs.writeFileSync(tmp, contents, { mode });
    // `mode` on writeFileSync only applies when the file is created, and the
    // rename carries the temp file's mode onto the target — so tightening here
    // also tightens a target that already existed with looser permissions.
    fs.chmodSync(tmp, mode);
    fs.renameSync(tmp, file);
  } catch (error) {
    fs.rmSync(tmp, { force: true });
    throw error;
  }
  return file;
}

export function parseOptions(args) {
  const flags = new Set();
  const values = new Map();
  const positionals = [];

  for (let i = 0; i < args.length; i += 1) {
    const arg = args[i];
    if (arg.startsWith("--")) {
      const [name, inlineValue] = arg.slice(2).split(/=(.*)/s, 2);
      if (inlineValue !== undefined) {
        values.set(name, inlineValue);
      } else if (i + 1 < args.length && !args[i + 1].startsWith("-") && ["set", "title", "content", "slug", "tag", "client", "service", "host", "dir", "name"].includes(name)) {
        const existing = values.get(name);
        const next = args[i + 1];
        values.set(name, existing === undefined ? next : Array.isArray(existing) ? [...existing, next] : [existing, next]);
        i += 1;
      } else {
        flags.add(name);
      }
    } else {
      positionals.push(arg);
    }
  }

  return { flags, values, positionals };
}

export async function askText(label, defaultValue = "", { required = false, secret = false } = {}) {
  if (!process.stdin.isTTY) {
    if (defaultValue || !required) return defaultValue;
    throw new Error(`${label} is required in non-interactive mode.`);
  }
  const rl = readline.createInterface({ input, output });
  try {
    while (true) {
      const shown = defaultValue ? ` [${secret ? "<hidden>" : defaultValue}]` : "";
      const value = (await rl.question(`${label}${shown}: `)).trim() || defaultValue;
      if (value || !required) return value;
      console.error("Required value.");
    }
  } finally {
    rl.close();
  }
}

export async function askBool(label, defaultValue = false) {
  if (!process.stdin.isTTY) return defaultValue;
  const rl = readline.createInterface({ input, output });
  try {
    const suffix = defaultValue ? "Y/n" : "y/N";
    const raw = (await rl.question(`${label} [${suffix}]: `)).trim().toLowerCase();
    if (!raw) return defaultValue;
    return ["y", "yes", "true", "1", "on"].includes(raw);
  } finally {
    rl.close();
  }
}

export function ensureRoot(cwd = process.cwd()) {
  if (!fs.existsSync(path.join(cwd, "docker-compose.yml"))) {
    throw new Error("Could not find docker-compose.yml. Run this command from an Archivum install directory.");
  }
  return cwd;
}

export function readStdinIfAvailable() {
  if (process.stdin.isTTY) return Promise.resolve("");
  return new Promise((resolve, reject) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => {
      data += chunk;
    });
    process.stdin.on("end", () => resolve(data));
    process.stdin.on("error", reject);
  });
}
