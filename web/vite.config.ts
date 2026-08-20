import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

/** Where the backend actually listens. Kept on 127.0.0.1 deliberately - see below. */
const BACKEND = process.env.VITE_PROXY_TARGET ?? "http://127.0.0.1:7860";

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Plain CSS, no PostCSS plugins. Declaring this inline stops Vite searching
  // parent directories for a postcss config, which otherwise lets an unrelated
  // config outside the repo break the build on some machines.
  css: { postcss: {} },
  server: {
    port: 5173,
    // Proxy the API so the browser only ever talks to one origin in dev, the
    // same as it does in the container. Two reasons this matters:
    //
    // 1. No CORS in development.
    // 2. It sidesteps an address-family trap on Windows. `localhost` resolves
    //    to ::1 before 127.0.0.1, and uvicorn bound to 0.0.0.0 listens on IPv4
    //    only - so a browser calling http://localhost:7860 gets connection
    //    refused while curl (which falls back to IPv4) succeeds. Proxying to
    //    an explicit 127.0.0.1 removes the ambiguity.
    proxy: {
      "/api": {
        target: BACKEND,
        changeOrigin: true,
      },
    },
  },
});
