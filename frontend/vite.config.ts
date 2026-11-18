import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In development the Vite server (port 5173) proxies API calls to FastAPI (port 8000),
// so the browser sees a single origin, the same as production.
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1", // loopback only: the dev server has known advisories (see issue tracker)
    port: 5173,
    proxy: {
      "/health": "http://127.0.0.1:8000",
      "/api": "http://127.0.0.1:8000",
    },
  },
});
