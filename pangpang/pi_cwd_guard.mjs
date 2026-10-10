// Cwd confinement for the bundled pi executor. Loaded with -e while
// --no-extensions suppresses every other extension. tool_call runs before the
// tool; a thrown handler also blocks, so uncertainty fails closed.
import { lstatSync, readlinkSync, realpathSync } from "node:fs";
import { homedir, platform } from "node:os";
import { isAbsolute, dirname, join, sep } from "node:path";
import { fileURLToPath } from "node:url";

const TOOLS = new Set(["read", "write", "edit", "grep", "find", "ls"]);
// pi 1.0.3 normalizePath unicode-space class (packages/coding-agent/src/utils/paths.ts).
const UNICODE_SPACES = /[\u00A0\u2000-\u200A\u202F\u205F\u3000]/g;

function inside(real, cwdReal) {
  const fold = platform() === "win32" ? (p) => p.toLowerCase() : (p) => p;
  const a = fold(real);
  const b = fold(cwdReal);
  if (a === b) return true;
  const prefix = b.endsWith(sep) ? b : b + sep;
  return a.startsWith(prefix);
}

function lexical(base, parts) {
  let cur = base;
  for (const name of parts) {
    if (name === "" || name === ".") continue;
    cur = name === ".." ? dirname(cur) : join(cur, name);
  }
  return cur;
}

function rootOf(abs) {
  if (abs.startsWith(sep)) return { start: sep, parts: abs.split(sep).slice(1) };
  const drive = /^[A-Za-z]:/.exec(abs);
  if (drive) {
    const rest = abs.slice(drive[0].length).replace(/^[\\/]/, "");
    return { start: drive[0] + sep, parts: rest.split(/[\\/]/) };
  }
  return { start: sep, parts: abs.split(/[\\/]/) };
}

// Walk one component at a time. realpath only the nearest existing ancestor;
// lstat the next name so a dangling symlink is seen instead of skipped. Do not
// lexically collapse ".." before that walk: link/../file must follow link.
function locate(start, parts) {
  let current = start;
  for (let i = 0; i < parts.length; i++) {
    const name = parts[i];
    if (name === "" || name === ".") continue;
    if (name === "..") {
      current = dirname(current);
      continue;
    }
    const next = current === sep ? sep + name : join(current, name);
    let st;
    try {
      st = lstatSync(next);
    } catch (error) {
      if (error && error.code === "ENOENT") return lexical(next, parts.slice(i + 1));
      throw error;
    }
    if (!st.isSymbolicLink()) {
      current = next;
      continue;
    }
    let linked;
    try {
      linked = realpathSync(next);
    } catch {
      const raw = readlinkSync(next);
      const rest = parts.slice(i + 1);
      if (isAbsolute(raw)) {
        const rooted = rootOf(raw);
        return locate(rooted.start, rooted.parts.concat(rest));
      }
      return locate(dirname(next), raw.split(/[\\/]/).concat(rest));
    }
    current = linked;
  }
  try {
    return realpathSync(current);
  } catch {
    return current;
  }
}

// Mirrors pi 1.0.3 normalizePath with the options resolveToCwd actually passes
// (path-utils.ts: normalizeUnicodeSpaces + stripAtPrefix, expandTilde default
// true, trim off). Same steps, same order. Re-check on any pi upgrade.
// Do not lexically collapse ".." here: the walk below is what keeps link/../file
// on the symlink. read's macOS filename fallbacks (AM/PM space, NFD, curly
// quote) stay in the same directory after this path is resolved, so they are
// not a second location.
function windowsShellPath(filePath) {
  if (!filePath.startsWith("/") || filePath.startsWith("//") || filePath.includes("\\")) return filePath;
  const match = /^\/(?:mnt\/|cygdrive\/)?([a-z])(?:\/(.*))?$/i.exec(filePath);
  if (!match) return filePath;
  const suffix = match[2] ? match[2].split("/").join("\\") : "";
  return match[1].toUpperCase() + ":\\" + suffix;
}

function normalizeToolPath(input) {
  let normalized = input.replace(UNICODE_SPACES, " ");
  if (normalized.startsWith("@")) normalized = normalized.slice(1);
  if (platform() === "win32") normalized = windowsShellPath(normalized);
  const home = homedir();
  if (normalized === "~") return home;
  if (normalized.startsWith("~/") || (platform() === "win32" && normalized.startsWith("~\\"))) {
    return join(home, normalized.slice(2));
  }
  if (/^file:\/\//.test(normalized)) return fileURLToPath(normalized);
  return normalized;
}

function realLocation(target, cwdReal) {
  if (isAbsolute(target)) {
    const rooted = rootOf(target);
    return locate(rooted.start, rooted.parts);
  }
  return locate(cwdReal, target.split(/[\\/]/));
}

export default function (pi) {
  pi.on("tool_call", (event) => {
    if (!TOOLS.has(event.toolName)) return;
    // read/write/edit/grep/find/ls name a location only in `path` (optional for
    // grep, find, and ls; omitted means cwd). grep `glob` and find `pattern`
    // are matchers under that path. edit `edits` is replacement text.
    const target = event.input && event.input.path;
    if (target == null || target === "") return;
    if (typeof target !== "string") return { block: true, reason: "path_outside_cwd" };
    let normalized;
    try {
      normalized = normalizeToolPath(target);
    } catch {
      return { block: true, reason: "path_unresolved" };
    }
    let cwdReal;
    try {
      cwdReal = realpathSync(process.cwd());
    } catch {
      return { block: true, reason: "cwd_unresolved" };
    }
    let real;
    try {
      real = realLocation(normalized, cwdReal);
    } catch {
      return { block: true, reason: "path_unresolved" };
    }
    if (!inside(real, cwdReal)) return { block: true, reason: "path_outside_cwd" };
  });
}
