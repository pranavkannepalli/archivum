#!/usr/bin/env node
import { printHelp } from "./util.js";
import { installCommand } from "./install.js";
import { updateCommand } from "./update.js";
import { uninstallCommand } from "./uninstall.js";
import { stackCommand } from "./stack.js";
import { mcpCommand } from "./mcp.js";
import { connectCommand } from "./connect.js";
import { wikiCommand } from "./api.js";
import { configCommand } from "./config.js";
import { recoveryCommand } from "./recovery.js";

async function main(argv) {
  const [command, ...args] = argv;
  if (!command || command === "--help" || command === "-h" || command === "help") {
    printHelp();
    return;
  }

  if (command === "install") return installCommand(args);
  if (command === "update") return updateCommand(args);
  if (command === "uninstall") return uninstallCommand(args);
  if (command === "stack") return stackCommand(args);
  if (command === "mcp") return mcpCommand(args);
  if (command === "connect") return connectCommand(args);
  if (command === "wiki") return wikiCommand(args);
  if (command === "config") return configCommand(args);
  if (command === "recovery") return recoveryCommand(args);

  throw new Error(`Unknown command: ${command}`);
}

main(process.argv.slice(2)).catch((error) => {
  console.error(error.message);
  process.exitCode = 1;
});
