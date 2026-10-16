import { createHash } from "node:crypto";
import { readdir, readFile, stat, writeFile } from "node:fs/promises";
import { dirname, relative, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const dashboardRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const repositoryRoot = resolve(dashboardRoot, "..");
const outputRoot = resolve(repositoryRoot, "src/ai_dev_loop/dashboard/web_dist");

async function filesBelow(directory) {
  const result = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = resolve(directory, entry.name);
    if (entry.isDirectory()) result.push(...(await filesBelow(path)));
    else if (entry.isFile()) result.push(path);
  }
  return result;
}

function isProductionInput(path) {
  const name = relative(dashboardRoot, path).replaceAll("\\", "/");
  if (!name.startsWith("src/")) return true;
  return !name.includes(".test.") && !name.endsWith("/test-fixtures.ts") && !name.endsWith("/test-setup.ts");
}

async function digest(path) {
  return createHash("sha256").update(await readFile(path)).digest("hex");
}

const fixedInputs = [
  "index.html",
  "package-lock.json",
  "package.json",
  "scripts/write-build-manifest.mjs",
  "tsconfig.json",
  "vite.config.ts",
].map((name) => resolve(dashboardRoot, name));
const sourceInputs = (await filesBelow(resolve(dashboardRoot, "src"))).filter(isProductionInput);
const inputs = {};
for (const path of [...fixedInputs, ...sourceInputs].sort()) {
  inputs[relative(repositoryRoot, path).replaceAll("\\", "/")] = await digest(path);
}

const outputs = {};
for (const name of ["assets/dashboard.css", "assets/dashboard.js", "index.html"]) {
  const path = resolve(outputRoot, name);
  outputs[name] = { sha256: await digest(path), size: (await stat(path)).size };
}

const manifest = { schemaVersion: 1, inputs, outputs };
await writeFile(
  resolve(outputRoot, "build-manifest.json"),
  `${JSON.stringify(manifest, null, 2)}\n`,
  "utf8",
);
