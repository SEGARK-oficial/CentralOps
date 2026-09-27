/**
 * ESLint 8 (devDeps: eslint + @typescript-eslint/* + react-hooks +
 * react-refresh + jsx-a11y — a última instalada na R4-9.1, `npm install -D`
 * com o lockfile atualizado).
 *
 * R2-9.2 começou estreito de propósito (só Hooks + 1 regra de segurança).
 * R4-9.1 amplia para `plugin:@typescript-eslint/recommended` (o plugin já
 * estava registrado, só não extendido) e `plugin:jsx-a11y/recommended` —
 * cobertura de acessibilidade estática que nenhum teste de componente
 * substitui (ex.: `alt` ausente, `<div onClick>` sem role, label ausente).
 * Achados reais corrigidos no código; regras com volume desproporcional para
 * o valor que agregam ficam desligadas ABAIXO, com justificativa — nunca em
 * silêncio.
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
  extends: ["plugin:@typescript-eslint/recommended", "plugin:jsx-a11y/recommended"],
  plugins: ["@typescript-eslint", "react-hooks", "react-refresh", "jsx-a11y"],
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
    // `_prefixo` (ou destructuring que só existe pra EXCLUIR uma propriedade
    // via `...rest`) já era a convenção do código antes deste plugin existir
    // — ex. `const { const: _removed, ...rest } = r` (RuleRow.tsx),
    // `const { category, ...t } = tile` num teste. Configurar em vez de
    // reescrever a convenção do zero.
    "@typescript-eslint/no-unused-vars": [
      "error",
      {
        argsIgnorePattern: "^_",
        varsIgnorePattern: "^_",
        destructuredArrayIgnorePattern: "^_",
        ignoreRestSiblings: true,
      },
    ],
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
    // ── jsx-a11y: 1 regra desligada, com justificativa (R4-9.1) ──────────────
    // `no-autofocus` supõe o caso ruim clássico: autofocus num <input> de
    // página carregada do zero, que desorienta quem navega por leitor de
    // tela. Os 7 usos daqui são todos formulário/modal que só existe porque o
    // PRÓPRIO usuário acabou de pedir (clicou em "Novo", abriu um diálogo) —
    // mover o foco pro 1º campo é exatamente o padrão WAI-ARIA de diálogo
    // recomendado, não o anti-padrão que a regra mira. Volume 7/7 = 100%
    // falso positivo para o uso real deste código; desligar em vez de
    // salpicar 7 `eslint-disable-next-line`.
    "jsx-a11y/no-autofocus": "off",
    // `label-has-associated-control` só procura o controle/texto acessível
    // até 2 níveis de profundidade por padrão (default da regra, não
    // documentado no schema) — raso demais pro padrão comum deste design
    // system: `<label><input/><div><div>título</div><p>descrição</p></div>
    // </label>` (radio/checkbox "card" com título+descrição). Ajustado pra
    // achar de verdade, não desligado — continua reprovando um <label>
    // genuinamente sem texto nenhum.
    "jsx-a11y/label-has-associated-control": ["error", { depth: 5 }],
  },
}
