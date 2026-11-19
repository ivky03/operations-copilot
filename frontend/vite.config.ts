import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// API port 8010 (not the common 8000) so it can run alongside other local projects.
const apiTarget = "http://127.0.0.1:8010";

// In development the Vite server (port 5173) proxies API calls to FastAPI (port 8010),
// so the browser sees a single origin, the same as production.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1", // loopback only: the dev server is a development tool, not for your network
    port: 5173,
    strictPort: true, // fail loudly if 5173 is taken, instead of silently picking another port
    proxy: {
      "/health": apiTarget,
      "/api": apiTarget,
    },
  },
});
