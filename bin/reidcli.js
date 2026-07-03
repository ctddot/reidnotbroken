#!/usr/bin/env node
"use strict";

const { spawnSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const pkgRoot = path.resolve(__dirname, "..");
const version = require(path.join(pkgRoot, "package.json")).version;
const cacheRoot = path.join(os.homedir(), ".reidcli", "npm");
const venvDir = path.join(cacheRoot, `venv-${version}`);
const isWindows = process.platform === "win32";
const binDir = isWindows ? "Scripts" : "bin";
const pythonExe = path.join(venvDir, binDir, isWindows ? "python.exe" : "python");
const reidExe = path.join(venvDir, binDir, isWindows ? "reidcli.exe" : "reidcli");

function run(command, args, options = {}) {
  return spawnSync(command, args, {
    stdio: options.stdio || "inherit",
    shell: false,
    env: process.env,
  });
}

function probe(command, args) {
  const result = spawnSync(
    command,
    [
      ...args,
      "-c",
      "import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)",
    ],
    { stdio: "ignore", shell: false }
  );
  return result.status === 0 ? { command, args } : null;
}

function findPython() {
  if (isWindows) {
    return (
      probe("py", ["-3.12"]) ||
      probe("py", ["-3.11"]) ||
      probe("python", []) ||
      probe("python3", [])
    );
  }
  return (
    probe("python3.12", []) ||
    probe("python3", []) ||
    probe("python", [])
  );
}

function ensureVenv() {
  if (fs.existsSync(reidExe)) return;

  const py = findPython();
  if (!py) {
    console.error("reidcli needs Python 3.11+ available on PATH.");
    process.exit(1);
  }

  fs.mkdirSync(cacheRoot, { recursive: true });

  const venv = run(py.command, [...py.args, "-m", "venv", venvDir]);
  if (venv.status !== 0) process.exit(venv.status || 1);

  const pipUpgrade = run(pythonExe, ["-m", "pip", "install", "--upgrade", "pip"]);
  if (pipUpgrade.status !== 0) process.exit(pipUpgrade.status || 1);

  const install = run(pythonExe, ["-m", "pip", "install", pkgRoot]);
  if (install.status !== 0) process.exit(install.status || 1);
}

ensureVenv();
const child = run(reidExe, process.argv.slice(2));
process.exit(child.status ?? 1);
