import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, URL } from "node:url";

function readAgentlogToken(): string | undefined {
  try {
    const tokenPath = path.join(os.homedir(), ".agentlog", "api_token");
    const text = fs.readFileSync(tokenPath, "utf8").trim();
    return text || undefined;
  } catch {
    return undefined;
  }
}

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:3000",
        configure: (proxy) => {
          proxy.on("proxyReq", (proxyReq) => {
            const token = readAgentlogToken();
            if (token) {
              proxyReq.setHeader("Authorization", `Bearer ${token}`);
            }
          });
        },
      },
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
