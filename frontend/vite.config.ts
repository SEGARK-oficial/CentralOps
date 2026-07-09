import { defineConfig } from "vite"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"
import svgr from "vite-plugin-svgr"
import path from "path"

// ── Enterprise edition routes seam  ──────────────────────────
// The Community build ships `@/ee/routes` (an EMPTY route set), so no EE screen or
// chunk is ever in the Community bundle. The Enterprise build overrides this module
// via `resolve.alias` (GitLab `ee_else_ce` pattern) to point `@/ee/routes` at the
// `@centralops/web-ee` overlay's real routes. Gating by build-time module override
// (not a runtime flag) is what keeps EE code out of the artifact. A real module
// (vs a virtual one) keeps the seam unit-testable.

export default defineConfig({
  plugins: [react, tailwindcss, svgr],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: process.env.VITE_BACKEND_URL || "http://localhost:8000",
        changeOrigin: true,
      },
    },
  },
})
