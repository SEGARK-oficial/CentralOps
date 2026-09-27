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
import {
  findingsInText,
  looksLikePortuguese,
  walkTsFiles,
  countBySignature,
  findOverages,
  baselineToCounts,
  countsToBaselineObject,
} from "../../scripts/lib/i18n-literals-core.mjs"

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

describe("R3-9.2 — catraca ampliada (ternário/&&, props de texto, setError, throw)", () => {
  it("meta-caso: PEGA um ternário JSX com os dois ramos em PT", () => {
    const injected = `<span>{ativo ? "Está ativo" : "Está inativo"}</span>`
    const findings = findingsInText(injected, "fake/Status.tsx")
    expect(findings.some((f) => f.snippet.includes("Está ativo") && f.snippet.includes("Está inativo"))).toBe(true)
  })

  it("NÃO pega um ternário que é a CHAVE de um t() (validado por check-i18n.mjs, não por aqui)", () => {
    const clean = `<span>{t(ativo ? "status.active" : "status.inactive")}</span>`
    // "status.active"/"status.inactive" não têm acento nem palavra de PT_WORDS
    // — mas o ponto real é que isto é escopo do check-i18n.mjs (paridade de
    // chave), não desta catraca; garantimos que não duplica achado nenhum.
    expect(findingsInText(clean, "fake/Clean.tsx").length).toBe(0)
  })

  it("meta-caso: PEGA um `&&` JSX com PT cru", () => {
    const injected = `<span>{hasError && "Campo obrigatório"}</span>`
    const findings = findingsInText(injected, "fake/Field.tsx")
    expect(findings.some((f) => f.snippet.includes("Campo obrigatório"))).toBe(true)
  })

  it("meta-caso: PEGA props de texto além de aria-label/title/placeholder/alt (label=, message=, description=, helperText=, emptyLabel=)", () => {
    const injected = `<Notice message="Falha ao salvar" /><EmptyState emptyLabel="Nenhum resultado encontrado" />`
    const findings = findingsInText(injected, "fake/Props.tsx")
    expect(findings.some((f) => f.snippet.includes("Falha ao salvar"))).toBe(true)
    expect(findings.some((f) => f.snippet.includes("Nenhum resultado encontrado"))).toBe(true)
  })

  it("meta-caso: PEGA setError(...) com fallback PT cru no ramo falso do ternário (o padrão real do ScopeSelector.tsx)", () => {
    const injected = `setError(e instanceof Error ? e.message : "Falha ao carregar scopes")`
    const findings = findingsInText(injected, "fake/ScopeSelector.tsx")
    expect(findings.some((f) => f.snippet.includes("Falha ao carregar scopes"))).toBe(true)
  })

  it("meta-caso: PEGA throw new Error(\"...\") com PT cru (o padrão real do api.ts)", () => {
    const injected = `if (!response.ok) throw new Error("Falha ao exportar CSV da auditoria")`
    const findings = findingsInText(injected, "fake/api.ts")
    expect(findings.some((f) => f.snippet.includes("Falha ao exportar CSV da auditoria"))).toBe(true)
  })

  it("NÃO pega setError(t(\"...\")) nem throw new Error(i18n.t(\"...\")) — já passam por t()", () => {
    const clean = `
      setError(t("errors.generic"))
      throw new Error(i18n.t("alerts:history.errors.auditExportFailed"))
    `
    expect(findingsInText(clean, "fake/Clean2.tsx")).toEqual([])
  })
})

describe("R4-9.3 — atribuição de string PT a variável de mensagem (message =, error =, msg =)", () => {
  it("meta-caso: PEGA `const message = \"...\"` (declaração, não chamada de setter)", () => {
    const injected = `const message = "Falha ao carregar organizações"`
    const findings = findingsInText(injected, "fake/loadOrgs.ts")
    expect(findings.some((f) => f.snippet.includes("Falha ao carregar organizações"))).toBe(true)
  })

  it("meta-caso: PEGA `error = \"...\"` (reatribuição simples, sem const/let)", () => {
    const injected = `error = "Token inválido"`
    const findings = findingsInText(injected, "fake/token.ts")
    expect(findings.some((f) => f.snippet.includes("Token inválido"))).toBe(true)
  })

  it("meta-caso: PEGA `let msg = \"...\"` e identificadores compostos (errorMessage =, feedbackMsg =)", () => {
    const injected = `
      let msg = "Nenhum resultado encontrado"
      const errorMessage = "Sessão expirada"
      let feedbackMsg = "Alterações salvas com sucesso"
    `
    const findings = findingsInText(injected, "fake/compound.ts")
    expect(findings.some((f) => f.snippet.includes("Nenhum resultado encontrado"))).toBe(true)
    expect(findings.some((f) => f.snippet.includes("Sessão expirada"))).toBe(true)
    expect(findings.some((f) => f.snippet.includes("Alterações salvas com sucesso"))).toBe(true)
  })

  it("NÃO pega comparação (`===`/`==`) nem `setError(...)`/prop JSX já cobertos por outra regra (sem duplicar por engano no mesmo achado)", () => {
    const clean = `
      if (error === "algum valor") return
      const message = t("errors.generic")
    `
    expect(findingsInText(clean, "fake/CleanAssign.ts")).toEqual([])
  })

  it("NÃO pega identificador que só TERMINA com letras parecidas sem ser message/error/msg de verdade (\"errors\" no plural, com 's' sobrando)", () => {
    const clean = `errors = ["Falha ao processar"]`
    // É array, não string — looksLikePortuguese/regex de string simples não
    // deveria casar aqui (não há `"..."` logo após `=`, e sim `[`).
    expect(findingsInText(clean, "fake/ArrayAssign.ts")).toEqual([])
  })
})

describe("R3-9.2 — assinatura do baseline SEM linha (defeito da Rodada 3)", () => {
  // Repro exata do defeito: `Tabs.tsx:43` virou `:44` só por uma linha
  // inserida ACIMA do achado, sem literal novo nenhum — e a Sub 8 teve que
  // reajustar o baseline à mão. A assinatura não pode carregar o número da
  // linha; só arquivo + trecho.
  const SOURCE_V1 = `
    function useTabsCtx(component) {
      const ctx = useContext(TabsContext)
      if (!ctx) {
        throw new Error(\`<\${component}> precisa estar dentro de <Tabs>\`)
      }
      return ctx
    }
  `
  // A MESMA lógica, com uma linha em branco inserida acima — desloca a linha
  // do achado em 1, mas o TRECHO é idêntico.
  const SOURCE_V2_LINHA_DESLOCADA = `

    function useTabsCtx(component) {
      const ctx = useContext(TabsContext)
      if (!ctx) {
        throw new Error(\`<\${component}> precisa estar dentro de <Tabs>\`)
      }
      return ctx
    }
  `

  it("a assinatura NÃO inclui o número da linha", () => {
    const [finding] = findingsInText(SOURCE_V1, "fake/Tabs.tsx")
    expect(finding.signature).toBe("fake/Tabs.tsx:precisa estar dentro de")
    expect(finding.signature).not.toMatch(/:\d+:/)
  })

  it("POSITIVO (a regra funciona): deslocar a linha do mesmo achado NÃO gera excedente contra o baseline", () => {
    const before = countBySignature(findingsInText(SOURCE_V1, "fake/Tabs.tsx"))
    const baseline = baselineToCounts(countsToBaselineObject(before))

    const afterShift = countBySignature(findingsInText(SOURCE_V2_LINHA_DESLOCADA, "fake/Tabs.tsx"))
    expect(findOverages(afterShift, baseline)).toEqual([])
  })

  it("NEGATIVO: um literal NOVO (assinatura ausente do baseline) gera excedente e falha", () => {
    const baseline = baselineToCounts(countsToBaselineObject(countBySignature(findingsInText(SOURCE_V1, "fake/Tabs.tsx"))))

    const withNewLiteral = `
      ${SOURCE_V1}
      export function Outro() {
        return <button>Salvar alterações</button>
      }
    `
    const current = countBySignature(findingsInText(withNewLiteral, "fake/Tabs.tsx"))
    const overages = findOverages(current, baseline)
    expect(overages.length).toBe(1)
    expect(overages[0].signature).toBe("fake/Tabs.tsx:Salvar alterações")
  })

  it("NEGATIVO: uma 2ª ocorrência do MESMO texto no mesmo arquivo também conta como excedente (contagem, não presença)", () => {
    const baseline = baselineToCounts(countsToBaselineObject(countBySignature(findingsInText(SOURCE_V1, "fake/Tabs.tsx"))))

    // A MESMA frase de erro, duplicada em outra função do mesmo arquivo —
    // um `Set` de presença diria "assinatura já existe" e deixaria passar;
    // contando por ocorrência, a 2ª cópia é uma unidade NOVA.
    const duplicated = `${SOURCE_V1}\n${SOURCE_V1}`
    const current = countBySignature(findingsInText(duplicated, "fake/Tabs.tsx"))
    const overages = findOverages(current, baseline)
    expect(overages.length).toBe(1)
    expect(overages[0]).toMatchObject({ current: 2, baseline: 1, extra: 1 })
  })

  it("baselineToCounts lê o formato ANTIGO (array de strings) por compatibilidade de leitura", () => {
    const legacyArray = ["fake/A.tsx:algum texto", "fake/A.tsx:algum texto", "fake/B.tsx:outro"]
    const counts = baselineToCounts(legacyArray)
    expect(counts.get("fake/A.tsx:algum texto")).toBe(2)
    expect(counts.get("fake/B.tsx:outro")).toBe(1)
  })
})
