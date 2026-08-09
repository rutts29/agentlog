/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        background: "var(--background)",
        card: "var(--card)",
        popover: "var(--popover)",
        border: "var(--border)",
        muted: "var(--muted)",
        foreground: "var(--foreground)",
        "muted-foreground": "var(--muted-foreground)",
        "faint-foreground": "var(--faint-foreground)",
        primary: "var(--primary)",
        ring: "var(--ring)",
        "status-ok": "var(--status-ok)",
        "status-warn": "var(--status-warn)",
        "status-error": "var(--status-error)",
        "status-info": "var(--status-info)",
        harness: {
          codex: "var(--harness-codex)",
          claude: "var(--harness-claude)",
          cursor: "var(--harness-cursor)",
          warp: "var(--harness-warp)",
          other: "var(--harness-other)",
        },
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      borderRadius: {
        card: "8px",
        control: "6px",
      },
    },
  },
  plugins: [],
};
