/**
 * Meta-teste do núcleo de `scripts/check-i18n.mjs` — cobre especificamente o
 * suporte a `useTranslation([...])` (array de namespaces) que o orquestrador
 * adicionou nesta rodada: a semântica real do `getFixedT(lng, [a, b])` do
 * i18next é "a chave vale se existir em QUALQUER namespace da lista", e antes
 * dessa mudança um array caía sempre em "common" (falso positivo de "no
 * catalog entry" pra todo `t()` do arquivo).
 */
import {
  extractDefaultNamespaces,
  keyResolvesInAnyNamespace,
  resolveNamespaceCandidates,
} from "../../scripts/lib/i18n-check-core.mjs"

describe("extractDefaultNamespaces", () => {
  it("useTranslation(\"ns\") (string) — um namespace só", () => {
    expect(extractDefaultNamespaces('const { t } = useTranslation("config")')).toEqual(["config"])
  })

  it("useTranslation([...]) (array) — todos os namespaces da lista", () => {
    expect(
      extractDefaultNamespaces('const { t } = useTranslation(["dashboard", "config"])'),
    ).toEqual(["dashboard", "config"])
  })

  it("sem useTranslation() no arquivo — cai no default do i18next (\"common\")", () => {
    expect(extractDefaultNamespaces("export const x = 1")).toEqual(["common"])
  })

  it("array vazio useTranslation([]) — cai em \"common\" (nunca lista vazia)", () => {
    expect(extractDefaultNamespaces('useTranslation([])')).toEqual(["common"])
  })
})

describe("keyResolvesInAnyNamespace — positivo e negativo do array", () => {
  const FAKE_CATALOGS: Record<string, Set<string>> = {
    dashboard: new Set(["kpis.total"]),
    config: new Set(["page.title", "page.email.loadError"]),
  }
  const getCatalogKeys = (ns: string) => FAKE_CATALOGS[ns]

  it("POSITIVO: a chave existe no 2º namespace da lista — resolve (getFixedT com array)", () => {
    expect(keyResolvesInAnyNamespace(["dashboard", "config"], "page.title", getCatalogKeys)).toBe(true)
  })

  it("POSITIVO: a chave existe no 1º namespace da lista — resolve", () => {
    expect(keyResolvesInAnyNamespace(["dashboard", "config"], "kpis.total", getCatalogKeys)).toBe(true)
  })

  it("NEGATIVO: a chave não existe em NENHUM namespace da lista — não resolve", () => {
    expect(keyResolvesInAnyNamespace(["dashboard", "config"], "nao.existe.em.lugar.nenhum", getCatalogKeys)).toBe(
      false,
    )
  })

  it("NEGATIVO: namespace inexistente na lista não quebra a checagem dos outros", () => {
    expect(keyResolvesInAnyNamespace(["namespace-fantasma", "config"], "page.title", getCatalogKeys)).toBe(true)
    expect(keyResolvesInAnyNamespace(["namespace-fantasma"], "page.title", getCatalogKeys)).toBe(false)
  })

  it("sufixo de plural/contexto (key_algumacoisa) ainda resolve a partir da chave base", () => {
    const catalogs: Record<string, Set<string>> = { schedules: new Set(["detections.list.table.occurrenceCount_one"]) }
    expect(
      keyResolvesInAnyNamespace(["schedules"], "detections.list.table.occurrenceCount", (ns) => catalogs[ns]),
    ).toBe(true)
  })
})

describe("resolveNamespaceCandidates", () => {
  it("t(\"ns:chave\") com prefixo explícito ignora os namespaces default do arquivo", () => {
    expect(resolveNamespaceCandidates("schedules:detections.list.pageTitle", ["dashboard", "config"])).toEqual({
      nsList: ["schedules"],
      key: "detections.list.pageTitle",
    })
  })

  it("t(\"chave\") sem prefixo usa a lista de namespaces default do arquivo", () => {
    expect(resolveNamespaceCandidates("page.title", ["dashboard", "config"])).toEqual({
      nsList: ["dashboard", "config"],
      key: "page.title",
    })
  })
})
