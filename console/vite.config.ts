import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// Dev server on :5173 proxies /admin to the Admin API on :8100, so the browser talks
// to one origin (no CORS needed in development either).
export default defineConfig({
  plugins: [react()],
  server: {
    // Explicit IPv4 loopback: by default Node can bind only ::1 (IPv6), which makes
    // http://127.0.0.1:5173 refuse connections while http://localhost:5173 works.
    // Binding 127.0.0.1 serves both names and keeps the dev server off the network.
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: { "/admin": "http://127.0.0.1:8100" },
  },
  test: { environment: "jsdom", globals: false },
});
