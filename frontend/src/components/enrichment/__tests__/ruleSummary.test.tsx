/**
 * Resumo legível de uma regra.
 *
 * A regra é escrita no vocabulário do motor, e nenhum campo isolado diz o que
 * ela faz: revisar quatro regras exigia ler doze campos e montar a frase de
 * cabeça. O resumo é o que responde "é esta a regra?" antes de o olho descer
 * para o formulário.
 */

import { render, screen } from "@testing-library/react"
import { describe, it, expect, beforeAll } from "vitest"
import type { TFunction } from "i18next"
import { RuleSummary, labelForPath, summarizeRule } from "@/components/enrichment/ruleSummary"
import type { EnricherCatalogItem, EnrichmentRule } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

/** O `t` do catálogo real: o resumo é traduzido, não texto fixo. */
const t = ((k: string) => i18n.t(k, { ns: "enrichment" })) as unknown as TFunction

const enrichers = [
  { name: "table_cidr", label: "Tabela do cliente (CIDR)" },
  { name: "virustotal", label: "VirusTotal" },
] as EnricherCatalogItem[]

function rule(over: Partial<EnrichmentRule> = {}): EnrichmentRule {
  return {
    id: "regra-site",
    enricher: "table_cidr",
    table: "plano-de-rede",
    key: { source: "normalized.src_endpoint.ip", kind: "ip" },
    outputs: [{ from: "site", target: "_centralops.enrichment.src.site" }],
    tags: [],
    on_miss: "skip",
    ...over,
  }
}

describe("labelForPath", () => {
  it("traduz os caminhos que de fato aparecem", () => {
    expect(labelForPath("normalized.src_endpoint.ip", t)).toBe("IP de origem")
    expect(labelForPath("normalized.file.hashes[0].value", t)).toBe("hash do arquivo")
  })

  it("mostra o caminho CRU quando não conhece", () => {
    // Inventar uma tradução para um campo que não está no mapa seria pior que
    // mostrar o caminho: o operador confiaria num nome errado.
    expect(labelForPath("normalized.coisa.exotica", t)).toBe("normalized.coisa.exotica")
  })
})

describe("summarizeRule", () => {
  it("prefere o nome da TABELA ao do enricher", () => {
    // Duas regras com `table_cidr` consultando tabelas diferentes fazem coisas
    // diferentes, e é o nome da tabela que o operador reconhece.
    expect(summarizeRule(rule(), enrichers, t).via).toBe("plano-de-rede")
  })

  it("cai na fonte, e depois no enricher, quando não há tabela", () => {
    expect(
      summarizeRule(rule({ enricher: "virustotal", table: null, source: "vt-prod" }), enrichers, t).via,
    ).toBe("vt-prod")
    expect(
      summarizeRule(rule({ enricher: "virustotal", table: null, source: null }), enrichers, t).via,
    ).toBe("VirusTotal")
  })

  it("nomeia o destino sem o prefixo obrigatório", () => {
    // O prefixo é o mesmo em toda regra; repeti-lo em cada linha da lista
    // ocuparia espaço sem distinguir nada.
    expect(summarizeRule(rule(), enrichers, t).writes).toEqual(["src.site"])
  })

  it("descreve a condição e o comportamento de miss", () => {
    const s = summarizeRule(
      rule({ when: { lacks_tag: ["asset_known"] }, on_miss: "tag" }),
      enrichers,
      t,
    )
    expect(s.when).toMatch(/ainda não tiver a marca/i)
    expect(s.onMiss).toMatch(/marca quando não encontra/i)
  })

  it("regra que roda sempre não inventa condição", () => {
    const s = summarizeRule(rule({ when: null }), enrichers, t)
    expect(s.when).toBeNull()
    expect(s.onMiss).toBeNull()
  })
})

describe("RuleSummary", () => {
  it("rende a frase inteira", () => {
    render(<RuleSummary rule={rule()} enrichers={enrichers} />)
    expect(screen.getByText("IP de origem")).toBeInTheDocument()
    expect(screen.getByText("plano-de-rede")).toBeInTheDocument()
    expect(screen.getByText("src.site")).toBeInTheDocument()
  })
  it("o resumo é TRADUZIDO, não texto fixo", async () => {
    // Frase fixa em português já apareceu uma vez nesta feature, vinda do
    // backend, e produziu uma tela metade em inglês e metade em português.
    await i18n.changeLanguage("en")
    const tEn = ((k: string) => i18n.t(k, { ns: "enrichment" })) as unknown as TFunction
    expect(labelForPath("normalized.src_endpoint.ip", tEn)).toBe("source IP")
    expect(summarizeRule(rule({ on_miss: "tag" }), enrichers, tEn).onMiss).toMatch(
      /marks when nothing is found/i,
    )
    await i18n.changeLanguage("pt")
  })
})
