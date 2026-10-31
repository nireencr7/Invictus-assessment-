/// <reference types="vitest/config" />
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The console is a static app. In development Vite proxies /v1 to the local API; in a container
// nginx does the same (see ui/nginx/default.conf.template), so the browser only ever talks to
// one origin and the API needs no CORS.
const API = process.env.API_UPSTREAM ?? "http://localhost:8000";

export default defineConfig({
  base: "./",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: { "/v1": API, "/readyz": API },
  },
  build: { outDir: "dist", sourcemap: true },
  test: { environment: "node", include: ["src/**/*.test.ts"] },
});
