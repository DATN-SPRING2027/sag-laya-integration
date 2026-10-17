const { spawnSync } = require("node:child_process");
const { cpSync, existsSync, mkdirSync } = require("node:fs");
const { join, resolve } = require("node:path");

const standaloneDir = resolve(".next/standalone");
const staticDir = resolve(".next/static");
if (!existsSync(join(standaloneDir, "server.js")) || !existsSync(staticDir)) {
  throw new Error("Run npm run build before npm start.");
}

mkdirSync(join(standaloneDir, ".next"), { recursive: true });
cpSync(staticDir, join(standaloneDir, ".next/static"), { recursive: true });
if (existsSync("public")) {
  cpSync("public", join(standaloneDir, "public"), { recursive: true });
}

const { error, status } = spawnSync(process.execPath, ["server.js"], {
  cwd: standaloneDir,
  env: process.env,
  stdio: "inherit",
});
if (error) throw error;
process.exit(status ?? 1);
