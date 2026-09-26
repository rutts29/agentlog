import assert from "node:assert/strict";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const webRoot = path.resolve(scriptDir, "..");
const sourceRoot = path.join(webRoot, "src");

function filesUnder(root) {
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const absolute = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...filesUnder(absolute));
    else files.push(absolute);
  }
  return files;
}

const sourceFiles = filesUnder(sourceRoot).filter((file) => /\.(css|ts|tsx)$/.test(file));
const source = sourceFiles.map((file) => fs.readFileSync(file, "utf8")).join("\n");
const pkg = JSON.parse(fs.readFileSync(path.join(webRoot, "package.json"), "utf8"));

for (const required of [
  "src/lib/commandFloor.ts",
  "src/lib/missionReplay.ts",
  "src/views/CommandFloorView.tsx",
  "src/components/command-grid/CommandFloor.tsx",
  "src/components/command-grid/CrewSprite.tsx",
  "src/components/command-grid/MissionArena.tsx",
  "src/components/command-grid/ExecutionTimeline.tsx",
  "src/components/command-grid/RuntimeGrid.tsx",
  "src/components/command-grid/SectorMap.tsx",
  "src/components/DiagnosticLoader.tsx",
]) {
  assert.ok(fs.existsSync(path.join(webRoot, required)), `missing ${required}`);
}

for (const forbidden of [
  "LoadingOrb",
  "LiveOrb",
  "thinking-orb",
  "live-orb",
  "ConstellationGraph",
]) {
  assert.equal(source.includes(forbidden), false, `retired UI token remains: ${forbidden}`);
}

for (const dependency of ["react-force-graph-2d", "d3-force-3d"]) {
  assert.equal(Boolean(pkg.dependencies?.[dependency]), false, `retired dependency remains: ${dependency}`);
}

for (const label of [
  "Command deck",
  "Command floor",
  "Active missions",
  "Trace search",
  "Sector map",
  "Council review",
  "Holotrace · Transcript",
  "indexed multi-session mission replay",
  "Execution timeline",
]) {
  assert.ok(source.includes(label), `missing approved interface label: ${label}`);
}

const distAssets = path.join(webRoot, "dist", "assets");
if (fs.existsSync(distAssets)) {
  const built = fs.readdirSync(distAssets).join("\n");
  assert.equal(/ConstellationGraph|force-graph/i.test(built), false, "retired graph shipped in dist");
}

console.log(`Command Grid contract verified across ${sourceFiles.length} source files.`);
