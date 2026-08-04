import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev the SPA runs on its own port and proxies the API (incl. the SSE
// stream) to the agent's monitor server on 127.0.0.1:4319. In production the
// agent serves the built `dist/` itself, so these are same-origin.
const AGENT = "http://127.0.0.1:4319";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 4320,
    proxy: {
      "/api": { target: AGENT, changeOrigin: true },
    },
  },
});
