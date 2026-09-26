/**
 * ESLint 8 (devDeps já trazem eslint + @typescript-eslint/* + react-hooks +
 * react-refresh — nenhuma dependência nova). Escopo deliberadamente estreito
 * (R2-9.2): regras de corretude de Hooks + uma regra de segurança central,
 * não um `eslint:recommended` genérico — isto não é uma reescrita de lint,
 * é o gate específico que a Rodada 2 pediu. Rodar mais regras é trabalho de
 * uma fase própria, com triagem dedicada.
 */
module.exports = {
  root: true,
  env: { browser: true, es2022: true, node: true },
  parser: "@typescript-eslint/parser",
  parserOptions: {
    ecmaVersion: "latest",
    sourceType: "module",
    ecmaFeatures: { jsx: true },
  },
  // "@typescript-eslint" fica registrado (mesmo sem `extends` do seu preset)
  // para que os `eslint-disable-next-line @typescript-eslint/no-unused-vars`
  // e `@typescript-eslint/no-explicit-any` já existentes no código apontem
  // pra regras REAIS, e não fiquem órfãos.
  plugins: ["@typescript-eslint", "react-hooks", "react-refresh"],
  ignorePatterns: [
    "dist",
    "dist-*",
    "node_modules",
    // Cópia descartável do overlay EE (rsync a partir do worktree do EE,
    // nunca editada aqui) — tem o próprio lint no repo do EE.
    "web-ee",
    "coverage",
    "*.config.ts",
    "*.config.js",
  ],
  rules: {
    // ── Corretude de Hooks (o pedido central da R2-9.2) ──────────────────────
    "react-hooks/rules-of-hooks": "error",
    // "warn": o volume de exhaustive-deps real no código (ver relatório) é
    // grande o bastante pra virar uma fase de triagem própria — a maioria dos
    // casos hoje já tem `eslint-disable-next-line` comentado com o motivo.
    "react-hooks/exhaustive-deps": "warn",
    // ── Segurança ────────────────────────────────────────────────────────────
    // Regra nativa do ESLint (sem plugin) — barra `href="javascript:..."`.
    // `eslint-plugin-react` (que traria `react/jsx-no-target-blank`) não está
    // instalado; a Rodada 2 pediu para NÃO adicionar dependência por isto.
    "no-script-url": "error",
  },
}
