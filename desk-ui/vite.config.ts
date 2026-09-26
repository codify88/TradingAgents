import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// `npm run dev` proxies the API to a Desk server started with
// `tradingagents desk --port 8766 --no-open`; `npm run build` writes dist/,
// which that server then serves itself.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: { "/api": { target: "http://localhost:8766", changeOrigin: false } },
  },
  build: { outDir: "dist", sourcemap: true },
  test: { environment: "jsdom", include: ["src/**/*.test.{ts,tsx}"] },
} as never);
