/**
 * Diferença entre o rascunho e a versão vigente de uma política.
 *
 * Publicar SUBSTITUI a lista inteira de regras — não faz mesclagem. Uma regra
 * removida sem querer não gera erro: ela simplesmente para de casar, e nos
 * painéis aparece com zero em tudo, indistinguível de "nenhum evento tinha o
 * campo". Este componente é a única chance de perceber isso antes.
 */

import { render, screen } from "@testing-library/react"
import { describe, it, expect, beforeAll } from "vitest"
import { PolicyDiff, diffRules } from "@/components/enrichment/PolicyDiff"
import type { EnrichmentRule } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

function rule(id: string, over: Partial<EnrichmentRule> = {}): EnrichmentRule {
  return {
    id,
    enricher: "table_cidr",
    table: "rede",
    key: { source: "normalized.src_endpoint.ip", kind: "ip" },
    outputs: [{ from: "site", target: "_centralops.enrichment.src.site" }],
    tags: [],
    on_miss: "skip",
    ...over,
  }
}

describe("diffRules", () => {
  it("compara por id, não por posição", () => {
    // Reordenar não é alteração de conteúdo. Tratar como se fosse encheria o
    // diff de ruído e esconderia a remoção, que é o que importa.
    const a = [rule("um"), rule("dois")]
    const b = [rule("dois"), rule("um")]
    const d = diffRules(a, b)

    expect(d.added).toEqual([])
    expect(d.removed).toEqual([])
    expect(d.changed).toEqual([])
    // Mas a ordem É reportada: ela decide a cascata de alternativas, em que
    // uma regra marca uma tag e a seguinte reage a ela.
    expect(d.reordered).toBe(true)
  })

  it("adição não conta como reordenação", () => {
    const d = diffRules([rule("um")], [rule("zero"), rule("um")])
    expect(d.added).toEqual(["zero"])
    expect(d.reordered).toBe(false)
  })

  it("detecta alteração de conteúdo com o mesmo id", () => {
    const d = diffRules(
      [rule("um")],
      [rule("um", { on_miss: "tag" })],
    )
    expect(d.changed).toEqual(["um"])
    expect(d.added).toEqual([])
  })

  it("lista o que SAI da política", () => {
    const d = diffRules([rule("um"), rule("dois")], [rule("um")])
    expect(d.removed).toEqual(["dois"])
  })
})

describe("PolicyDiff", () => {
  it("diz explicitamente quando não há nada a publicar", () => {
    // Sem isto, o botão de publicar ficaria armado e o operador criaria uma
    // versão idêntica à anterior, poluindo o histórico que serve ao rollback.
    render(<PolicyDiff published={[rule("um")]} draft={[rule("um")]} />)
    expect(screen.getByTestId("policy-diff-empty")).toHaveTextContent(
      /igual à versão vigente/i,
    )
  })

  it("destaca a remoção com o nome da regra que sai", () => {
    render(
      <PolicyDiff published={[rule("um"), rule("vt-ip")]} draft={[rule("um")]} />,
    )
    const diff = screen.getByTestId("policy-diff")
    expect(diff).toHaveTextContent("vt-ip")
    expect(diff).toHaveTextContent(/−1 regra/)
  })

  it("mostra adição, alteração e remoção juntas", () => {
    render(
      <PolicyDiff
        published={[rule("fica"), rule("muda"), rule("sai")]}
        draft={[rule("fica"), rule("muda", { on_miss: "tag" }), rule("nova")]}
      />,
    )
    const diff = screen.getByTestId("policy-diff")
    expect(diff).toHaveTextContent("nova")
    expect(diff).toHaveTextContent("muda")
    expect(diff).toHaveTextContent("sai")
  })
})
