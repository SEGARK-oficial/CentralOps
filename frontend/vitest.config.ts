import path from "path"
import { defineConfig } from "vitest/config"
import react from "@vitejs/plugin-react"
import tailwindcss from "@tailwindcss/vite"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    // `i18next`/`react-i18next` inlined (processados pelo Vite, não
    // externalizados pro require nativo do Node): sem isto, `vi.resetModules()`
    // + `import()` dinâmico de `@/i18n` (usado em src/i18n/__tests__/
    // lazyLoading.test.ts) recarrega o módulo FONTE (`src/i18n/index.ts`), mas
    // reusa a MESMA instância singleton de `i18next` de node_modules em todo o
    // worker — pacotes externalizados ficam fora do grafo que `resetModules()`
    // invalida. `i18next.init()` faz merge de opções antigas (`ns`, `lng`) com
    // as novas nessa instância "velha", vazando estado entre testes que se
    // acham "frescos" — a causa raiz do flake de ~10–30% no arquivo acima.
    server: {
      deps: {
        inline: ["i18next", "react-i18next"],
      },
    },
  },
})
