import { copyFile, mkdir, readdir, rm } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const adapterRoot = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
const repositoryRoot = path.dirname(path.dirname(adapterRoot));
const packageNames = ["mijia_agent", "mijia_assistant"];

export async function syncPython() {
  for (const packageName of packageNames) {
    const sourceDirectory = path.join(repositoryRoot, "src", packageName);
    const outputDirectory = path.join(adapterRoot, "cloud-functions", "api", packageName);
    await rm(outputDirectory, { recursive: true, force: true });
    await copyPythonPackage(sourceDirectory, outputDirectory);
  }
}

async function copyPythonPackage(source, output) {
  await mkdir(output, { recursive: true });
  for (const entry of await readdir(source, { withFileTypes: true })) {
    const sourcePath = path.join(source, entry.name);
    const outputPath = path.join(output, entry.name);
    if (entry.isDirectory() && entry.name !== "__pycache__") {
      await copyPythonPackage(sourcePath, outputPath);
    } else if (entry.isFile() && entry.name.endsWith(".py")) {
      await copyFile(sourcePath, outputPath);
    }
  }
}

if (process.argv[1] === fileURLToPath(import.meta.url)) {
  await syncPython();
}
