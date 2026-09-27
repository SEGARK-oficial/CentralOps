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

// PERF-02: sem bloco `build`, o Vite 8 (motor rolldown) deixava react-i18next,
// i18next e os primitivos de UI caírem TODOS no mesmo chunk de entrada — um
// deploy de rótulo (só i18n) invalidava o cache do vendor de UI inteiro, e
// vice-versa. Os grupos abaixo separam por churn/tamanho: react-vendor muda
// raríssimo, i18n-vendor muda com upgrades da lib de tradução (não a cada
// string nova — essas vivem em src/i18n/locales, fora do bundle de vendor),
// ui-vendor agrupa os primitivos usados em quase toda tela.
export default defineConfig({
  plugins: [react(), tailwindcss(), svgr()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  build: {
    rolldownOptions: {
      output: {
        codeSplitting: {
          groups: [
            {
              name: "react-vendor",
              test: /node_modules[\\/](react|react-dom|scheduler|react-router|react-router-dom|@remix-run)[\\/]/,
              priority: 30,
            },
            {
              name: "i18n-vendor",
              test: /node_modules[\\/](i18next|react-i18next|i18next-browser-languagedetector)[\\/]/,
              priority: 20,
            },
            {
              name: "ui-vendor",
              test: /node_modules[\\/](tailwind-merge|clsx|class-variance-authority|@radix-ui)[\\/]/,
              priority: 10,
            },
          ],
        },
      },
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
