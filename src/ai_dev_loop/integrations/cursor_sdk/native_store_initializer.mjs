/** Configure the pinned SDK's native SQLite store before its original bridge starts. */

import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";

function requiredFlag(name) {
  const index = process.argv.indexOf(name);
  const value = index >= 0 ? process.argv[index + 1] : undefined;
  if (!value) {
    throw new Error(`Required bridge flag is missing: ${name}`);
  }
  return resolve(value);
}

const bridgeEntrypoint = resolve(process.argv[1] ?? "");
const bridgeRoot = resolve(dirname(bridgeEntrypoint), "../..");
const sdkModule = pathToFileURL(
  resolve(bridgeRoot, "node_modules/@cursor/sdk/dist/esm/index.js"),
).href;
const sqliteModule = pathToFileURL(
  resolve(bridgeRoot, "node_modules/@cursor/sdk/dist/esm/sqlite.js"),
).href;

const [{ configureCursorSdk }, { SqliteLocalAgentStore }] = await Promise.all([
  import(sdkModule),
  import(sqliteModule),
]);
const store = await SqliteLocalAgentStore.open({
  workspaceRef: requiredFlag("--workspace"),
  stateRoot: requiredFlag("--state-root"),
});
configureCursorSdk({ local: { store } });

// A public bridge shutdown closes its server and lets the event loop drain.
// Dispose the process-owned native handles at that boundary. Abrupt process
// termination remains fail-closed and relies on ordinary OS handle cleanup.
process.once("beforeExit", async () => {
  await store.dispose();
});
