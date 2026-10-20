import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Test/showcase tool: the dev server proxies /api/* to the Router, so the Router needs no CORS configuration.
const router = process.env.ROUTER_URL ?? "http://127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: { port: 5173, proxy: { "/api": { target: router, rewrite: (p) => p.replace(/^\/api/, "") } } },
});
