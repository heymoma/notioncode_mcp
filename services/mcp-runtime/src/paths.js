import fs from "node:fs";
import path from "node:path";

/**
 * Resolve a model-supplied path and keep it inside CODE_ROOT.
 *
 * Lexical containment alone is not enough: a symlink inside CODE_ROOT that
 * points outside it passes a prefix check but reads and writes elsewhere. Every
 * existing path component is therefore resolved through the real filesystem.
 */
export function resolveInsideRoot(root, input, { mustExist = false } = {}) {
  const candidate = path.resolve(root, String(input ?? "."));
  assertContained(root, candidate, input);
  const real = realpathOfNearestExisting(candidate);
  if (real !== null) {
    assertContained(realpathOrSelf(root), real, input);
  }
  if (mustExist && !fs.existsSync(candidate)) {
    throw new Error(`Path does not exist: ${input}`);
  }
  return candidate;
}

function assertContained(root, candidate, input) {
  if (candidate !== root && !candidate.startsWith(`${root}${path.sep}`)) {
    throw new Error(`Path is outside CODE_ROOT: ${input}`);
  }
}

function realpathOrSelf(target) {
  try {
    return fs.realpathSync(target);
  } catch {
    return target;
  }
}

/**
 * The real path of `target`, or of its closest existing ancestor.
 *
 * Writing a new file has to be checked against the directory that will hold it,
 * because the file itself does not exist yet.
 */
function realpathOfNearestExisting(target) {
  let current = target;
  for (let depth = 0; depth < 64; depth += 1) {
    try {
      const real = fs.realpathSync(current);
      return current === target ? real : path.join(real, path.relative(current, target));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
      const parent = path.dirname(current);
      if (parent === current) return null;
      current = parent;
    }
  }
  throw new Error(`Path is nested too deeply: ${target}`);
}

export function relativeToRoot(root, target) {
  return path.relative(root, target) || ".";
}
