/**
 * R4-6.3: `src/services/api.ts` (3.438 linhas) virou um barrel por domínio em
 * `src/services/api/*.ts` (`_core.ts` + um arquivo por marcador `// ── X ───`
 * do arquivo original, `index.ts` reagrupando tudo). Este arquivo continua
 * existindo — em vez de apagado — para que `@/services/api` resolva pro
 * MESMO caminho de arquivo de sempre (não uma resolução de diretório nova),
 * o que `vi.mock("@/services/api")` (44 arquivos de teste) e todo `import *
 * as api from "@/services/api"` continuam enxergando sem mudança nenhuma.
 */
export * from "./api/index"
