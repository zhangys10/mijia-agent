import test from "node:test";
import assert from "node:assert/strict";
import { access, readdir, readFile } from "node:fs/promises";
import path from "node:path";
import { syncPython } from "../scripts/sync-python.mjs";

async function pythonFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true, recursive: true });
  return entries
    .filter(entry => entry.isFile() && entry.name.endsWith(".py"))
    .map(entry => path.relative(directory, path.join(entry.parentPath, entry.name)))
    .sort();
}

test("Cloud Functions build syncs the canonical Python packages without cache files", async () => {
  await syncPython();
  const root = path.resolve(import.meta.dirname, "../../..");
  for (const packageName of ["mijia_agent", "mijia_assistant"]) {
    const source = path.join(root, "src", packageName);
    const output = path.join(root, "adapters/edgeone/cloud-functions/api", packageName);

    assert.deepEqual(await pythonFiles(source), await pythonFiles(output));
    for (const file of await pythonFiles(source)) {
      assert.equal(
        await readFile(path.join(output, file), "utf8"),
        await readFile(path.join(source, file), "utf8"),
      );
    }
  }
  const indexSource = await readFile(
    path.join(root, "adapters/edgeone/cloud-functions/api/index.py"),
    "utf8",
  );
  assert.match(indexSource, /sys\.path\.insert\(0, API_ROOT\)/);
  assert.match(indexSource, /from mijia_agent\.app import create_lifespan, register_routes/);
  assert.match(indexSource, /from mijia_agent\.config import Settings/);
});

test("EdgeOne deployment root co-locates Agent and Cloud Functions markers", async () => {
  const root = path.resolve(import.meta.dirname, "..");
  const config = JSON.parse(await readFile(path.join(root, "edgeone.json"), "utf8"));

  assert.equal(config.agents.framework, "openai-agents-sdk");
  assert.equal(config.agents.dir, "agents");
  await assert.doesNotReject(access(path.join(root, "agents", "ai-home", "index.ts")));
  await assert.doesNotReject(access(path.join(root, "cloud-functions", "api", "index.py")));
});
