#!/usr/bin/env node
"use strict";

// Thin alias: run the installed Python entry. No postinstall, no download.
const { spawn } = require("child_process");

const python = process.env.PANGPANG_PYTHON || (process.platform === "win32" ? "python" : "python3");
const child = spawn(python, ["-m", "pangpang.cli", ...process.argv.slice(2)], { stdio: "inherit" });

child.on("error", () => {
  process.stderr.write(
    "pangpang needs Python 3.11+ and `pip install pangpang` on PATH"
    + (process.env.PANGPANG_PYTHON ? " (PANGPANG_PYTHON=" + process.env.PANGPANG_PYTHON + ")" : "")
    + ".\n");
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code == null ? 1 : code);
});
