import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const sourceRoot = path.resolve(scriptDir, "../src");

function sourceFiles(root) {
  const files = [];
  for (const entry of fs.readdirSync(root, { withFileTypes: true })) {
    const absolute = path.join(root, entry.name);
    if (entry.isDirectory()) files.push(...sourceFiles(absolute));
    else if (/\.(ts|tsx)$/.test(entry.name)) files.push(absolute);
  }
  return files;
}

let changed = 0;
for (const file of sourceFiles(sourceRoot)) {
  if (file.endsWith("LoadingOrb.tsx") || file.endsWith("DiagnosticLoader.tsx")) continue;
  const before = fs.readFileSync(file, "utf8");
  const after = before
    .replace(
      /import \{ LoadingOrb \} from "@\/components\/LoadingOrb";/g,
      'import { DiagnosticLoader } from "@/components/DiagnosticLoader";',
    )
    .replace(/<LoadingOrb\b/g, "<DiagnosticLoader")
    .replace(/<\/LoadingOrb>/g, "</DiagnosticLoader>");
  if (after === before) continue;
  fs.writeFileSync(file, after);
  changed += 1;
}

console.log(`Command Grid migration updated ${changed} source file${changed === 1 ? "" : "s"}.`);

