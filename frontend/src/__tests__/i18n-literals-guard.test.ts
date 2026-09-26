/**
 * Meta-teste da catraca de literais PT (R2-9.3, `scripts/check-i18n-
 * literals.mjs`) — a lógica de detecção vive em `scripts/lib/i18n-literals-
 * core.mjs` (puro, sem I/O), importada aqui e pela CLI real.
 *
 * Regra do repo (negative-assert-passes-on-empty.md): todo assert NEGATIVO
 * precisa de um POSITIVO ao lado — sem isto, "nenhum literal encontrado"
 * passaria tanto por um código limpo quanto por um detector quebrado. Os
 * dois casos abaixo são o positivo: o detector PEGA um literal injetado, e a
 * varredura real do repo ACHA arquivos de verdade (não zero por caminho
 * errado).
 */
import { readFileSync } from "node:fs"
import { join } from "node:path"
import { findingsInText, looksLikePortuguese, walkTsFiles } from "../../scripts/lib/i18n-literals-core.mjs"

describe("catraca de literais PT — núcleo do detector", () => {
  it("meta-caso: PEGA um texto JSX cru em PT injetado", () => {
    const injected = `
      export function Broken() {
        return <button>Salvar alterações</button>
      }
    `
    const findings = findingsInText(injected, "fake/Broken.tsx")
    expect(findings.length).toBeGreaterThan(0)
    expect(findings.some((f) => f.snippet.includes("Salvar alterações"))).toBe(true)
  })

  it("meta-caso: PEGA um aria-label cru em PT injetado", () => {
    const injected = `<button aria-label="Fechar diálogo">x</button>`
    const findings = findingsInText(injected, "fake/Close.tsx")
    expect(findings.some((f) => f.snippet.includes("Fechar diálogo"))).toBe(true)
  })

  it("NÃO pega texto que já passa por t() (dentro de {})", () => {
    const clean = `<button aria-label={t("actions.close")}>{t("actions.save")}</button>`
    expect(findingsInText(clean, "fake/Clean.tsx")).toEqual([])
  })

  it("ignora comentários que mencionam PT/tags JSX na prosa (não é código real)", () => {
    const commented = `
      // Isto aqui não deveria ser uma "detecção" — é só um comentário em PT.
      /* <p> em vez de <h1>: outra prosa comentada com tags. */
      export const x = 1
    `
    expect(findingsInText(commented, "fake/Comment.tsx")).toEqual([])
  })

  it("looksLikePortuguese: acento sozinho já basta; ASCII puro em inglês não pega", () => {
    expect(looksLikePortuguese("Configuração")).toBe(true)
    expect(looksLikePortuguese("Settings")).toBe(false)
    expect(looksLikePortuguese("42")).toBe(false)
  })

  // Positivo (anti-vacuidade): prova que a varredura de arquivo REAL do
  // repositório encontra uma quantidade de arquivos consistente com o gate
  // do CLI (que falha abaixo de 50) — se o caminho ou a regex de exclusão
  // quebrassem, cairia pra 0 e o teste abaixo pegaria antes do CLI.
  it("a varredura real de `src/` encontra dezenas de arquivos elegíveis", () => {
    const srcRoot = join(process.cwd(), "src")
    const files = [...walkTsFiles(srcRoot)]
    expect(files.length).toBeGreaterThan(50)

    // E o conteúdo real lido bate com o que `findingsInText` espera (smoke
    // test: não lança, devolve um array).
    const sample = files[0]
    const findings = findingsInText(readFileSync(sample, "utf8"), "sample")
    expect(Array.isArray(findings)).toBe(true)
  })
})
