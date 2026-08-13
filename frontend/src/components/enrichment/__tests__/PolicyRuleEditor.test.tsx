/**
 * PolicyRuleEditor tests.
 *
 * Cobre:
 * - Estado vazio (nenhuma regra).
 * - Adicionar regra chama onChange com uma regra a mais.
 * - Remover regra chama onChange sem aquela regra.
 * - Editar campos de uma regra (id, chave) propaga via onChange.
 * - Adicionar output chama onChange com outputs+1.
 * - Seletor de tabela só aparece quando há tabelas disponíveis.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import { PolicyRuleEditor } from "../PolicyRuleEditor"
import type { EnricherCatalogItem, EnrichmentRule, EnrichmentTable } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const enrichers: EnricherCatalogItem[] = [
  {
    name: "table_cidr",
    label: "Tabela CIDR",
    category: "table",
    description: "",
    icon_id: null,
    docs_url: null,
    tier: "core",
    order: 0,
    mode: "local",
    key_kinds: ["ip"],
    supports_bulk: true,
    suggested_ttl_s: 3600,
    license: "core",
    egress: "none",
    required_secrets: [],
    output_fields: { site: "string", criticality: "string" },
  },
  {
    name: "virustotal",
    label: "VirusTotal",
    category: "threat_intel",
    description: "",
    icon_id: null,
    docs_url: null,
    tier: "core",
    order: 1,
    mode: "remote",
    key_kinds: ["ip", "domain", "file_hash"],
    supports_bulk: true,
    suggested_ttl_s: 3600,
    license: "core",
    egress: "third_party",
    required_secrets: ["VIRUSTOTAL_API_KEY"],
    output_fields: { reputation: "string", malicious_count: "number" },
  },
]

const tables: EnrichmentTable[] = [
  {
    id: "t1",
    organization_id: 1,
    name: "rede-corp",
    description: null,
    match_mode: "cidr",
    key_kind: "ip",
    current_version_id: "v1",
    entry_count: 10,
    approx_bytes: 100,
  },
]

function baseRule(overrides: Partial<EnrichmentRule> = {}): EnrichmentRule {
  return {
    id: "regra-1",
    enricher: "table_cidr",
    key: { source: "normalized.src_endpoint.ip", kind: "ip" },
    outputs: [{ from: "site", target: "_centralops.enrichment.src.site" }],
    tags: [],
    on_miss: "skip",
    ...overrides,
  }
}

describe("PolicyRuleEditor", () => {
  it("mostra estado vazio quando não há regras", () => {
    render(<PolicyRuleEditor rules={[]} enrichers={enrichers} tables={tables} onChange={vi.fn()} />)

    expect(screen.getByTestId("rules-empty")).toBeInTheDocument()
    expect(screen.queryByTestId("rule-card-0")).not.toBeInTheDocument()
  })

  it("adiciona uma regra ao clicar em adicionar", () => {
    const onChange = vi.fn()
    render(<PolicyRuleEditor rules={[]} enrichers={enrichers} tables={tables} onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-rule"))

    expect(onChange).toHaveBeenCalledTimes(1)
    const [added] = onChange.mock.calls[0][0]
    expect(added.enricher).toBe("table_cidr")
    expect(added.outputs).toHaveLength(1)
  })

  it("remove uma regra ao clicar no botão de remover", () => {
    const onChange = vi.fn()
    const rules = [baseRule({ id: "regra-1" }), baseRule({ id: "regra-2" })]
    render(<PolicyRuleEditor rules={rules} enrichers={enrichers} tables={tables} onChange={onChange} />)

    fireEvent.click(screen.getByTestId("remove-rule-0"))

    expect(onChange).toHaveBeenCalledWith([expect.objectContaining({ id: "regra-2" })])
  })

  it("edita o id da regra e propaga via onChange", () => {
    const onChange = vi.fn()
    const rules = [baseRule()]
    render(<PolicyRuleEditor rules={rules} enrichers={enrichers} tables={tables} onChange={onChange} />)

    fireEvent.change(screen.getByLabelText("Id da regra"), { target: { value: "regra-renomeada" } })

    expect(onChange).toHaveBeenCalledWith([expect.objectContaining({ id: "regra-renomeada" })])
  })

  it("adiciona um output ao clicar em adicionar saída", () => {
    const onChange = vi.fn()
    const rules = [baseRule()]
    render(<PolicyRuleEditor rules={rules} enrichers={enrichers} tables={tables} onChange={onChange} />)

    fireEvent.click(screen.getByTestId("add-output-0"))

    const [updated] = onChange.mock.calls[0][0]
    expect(updated.outputs).toHaveLength(2)
  })

  it("mostra o seletor de tabela apenas quando há tabelas disponíveis", () => {
    const rules = [baseRule()]
    const { rerender } = render(
      <PolicyRuleEditor rules={rules} enrichers={enrichers} tables={tables} onChange={vi.fn()} />,
    )
    expect(screen.getByText("Tabela")).toBeInTheDocument()

    rerender(<PolicyRuleEditor rules={rules} enrichers={enrichers} tables={[]} onChange={vi.fn()} />)
    expect(screen.queryByText("Tabela")).not.toBeInTheDocument()
  })
  // ── widgets anti-erro ────────────────────────────────────────────────────

  it("oferece os campos declarados pelo enricher em vez de texto livre", () => {
    // Nomear um campo que o provedor não devolve é miss silencioso, não erro:
    // a regra roda, não acha nada, e nada é escrito no evento.
    render(
      <PolicyRuleEditor rules={[baseRule()]} enrichers={enrichers} tables={tables} onChange={vi.fn()} />,
    )

    fireEvent.click(screen.getByLabelText("Campo do resultado"))
    expect(screen.getByRole("option", { name: /criticality/ })).toBeInTheDocument()
  })

  it("mantém o prefixo do target fora do alcance da digitação", () => {
    // Fora de `_centralops.enrichment.` o commit é 422 e, pior, o dado não fica
    // sob a redação de PII. O prefixo é rótulo, não campo.
    const onChange = vi.fn()
    render(
      <PolicyRuleEditor rules={[baseRule()]} enrichers={enrichers} tables={tables} onChange={onChange} />,
    )

    const suffix = screen.getByLabelText("Caminho depois do prefixo")
    expect(suffix).toHaveValue("src.site")

    fireEvent.change(suffix, { target: { value: "dst.site" } })
    expect(onChange.mock.calls[0][0][0].outputs[0].target).toBe("_centralops.enrichment.dst.site")
  })

  it("normaliza a tag digitada e sugere as que já existem", () => {
    const onChange = vi.fn()
    render(
      <PolicyRuleEditor
        rules={[baseRule({ tags: ["asset_known"] }), baseRule({ id: "regra-2", tags: [] })]}
        enrichers={enrichers}
        tables={tables}
        onChange={onChange}
      />,
    )

    const input = screen.getByTestId("tags-1").querySelector("input") as HTMLInputElement
    // "Ativo Crítico" e "ativo_critico" seriam tags DIFERENTES para o runtime.
    fireEvent.change(input, { target: { value: "Ativo Crítico" } })
    fireEvent.keyDown(input, { key: "Enter" })

    expect(onChange.mock.calls[0][0][1].tags).toEqual(["ativo_crítico"])
    // A tag da outra regra vira sugestão, para não reinventá-la com erro.
    expect(screen.getByTestId("tags-1").querySelector('option[value="asset_known"]')).toBeTruthy()
  })

  it("bloqueia on_miss=default quando nenhum output tem padrão", () => {
    render(
      <PolicyRuleEditor rules={[baseRule()]} enrichers={enrichers} tables={tables} onChange={vi.fn()} />,
    )

    fireEvent.click(screen.getByLabelText("Se não encontrar"))
    expect(screen.getByRole("option", { name: "default" })).toBeDisabled()
  })

  it("libera on_miss=default assim que um output ganha padrão", () => {
    render(
      <PolicyRuleEditor
        rules={[
          baseRule({
            outputs: [{ from: "site", target: "_centralops.enrichment.src.site", default: "desconhecido" }],
          }),
        ]}
        enrichers={enrichers}
        tables={tables}
        onChange={vi.fn()}
      />,
    )

    fireEvent.click(screen.getByLabelText("Se não encontrar"))
    expect(screen.getByRole("option", { name: "default" })).not.toBeDisabled()
  })

  // ── condicionais ─────────────────────────────────────────────────────────

  it("edita a condição da regra", () => {
    const onChange = vi.fn()
    render(
      <PolicyRuleEditor rules={[baseRule()]} enrichers={enrichers} tables={tables} onChange={onChange} />,
    )

    expect(screen.getByTestId("when-builder")).toBeInTheDocument()
    fireEvent.click(screen.getByLabelText("Quando aplicar"))
    fireEvent.click(screen.getByRole("option", { name: "Se o evento NÃO tiver a tag" }))

    expect(onChange.mock.calls[0][0][0].when).toEqual({ lacks_tag: [] })
  })

  it("monta o par tag/lacks_tag ao adicionar alternativa", () => {
    // A DSL não tem `or`: um gate tem exatamente uma chave. Alternativa se
    // escreve encadeando regras, e acertar a tag à mão é fácil de errar.
    const onChange = vi.fn()
    render(
      <PolicyRuleEditor
        rules={[baseRule({ tags: ["achou_no_cmdb"] })]}
        enrichers={enrichers}
        tables={tables}
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByTestId("add-alternative-0"))

    const next = onChange.mock.calls[0][0]
    expect(next).toHaveLength(2)
    expect(next[0].tags).toContain("achou_no_cmdb")
    expect(next[1].when).toEqual({ lacks_tag: ["achou_no_cmdb"] })
    expect(next[1].id).toBe("regra-1-alternativa")
  })
  // ── regressões da revisão adversarial ────────────────────────────────────

  it("não reusa o id de uma regra que já existe", () => {
    // Desde que o editor abre COM as regras da versão vigente, um contador de
    // módulo (que nasce em zero a cada carregamento de página) gerava
    // `regra-1` em cima da `regra-1` existente. O 422 só aparecia no publish.
    const onChange = vi.fn()
    render(
      <PolicyRuleEditor
        rules={[baseRule({ id: "regra-1" }), baseRule({ id: "regra-2" })]}
        enrichers={enrichers}
        tables={tables}
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByTestId("add-rule"))

    const ids = onChange.mock.calls[0][0].map((r: EnrichmentRule) => r.id)
    expect(new Set(ids).size).toBe(ids.length)
  })

  it("acumula os marcadores numa cascata de três níveis", () => {
    // `lacks_tag` é "pula se QUALQUER uma estiver presente" (applier.py:138).
    // Substituir o gate faria o 3º elo rodar mesmo com o 1º tendo resolvido,
    // e o compilador aceitaria: continua uma chave só.
    const onChange = vi.fn()
    const alternativa = baseRule({
      id: "cmdb-alternativa",
      tags: ["achou_no_opencti"],
      when: { lacks_tag: ["achou_no_cmdb"] },
    })
    render(
      <PolicyRuleEditor
        rules={[baseRule({ id: "cmdb", tags: ["achou_no_cmdb"] }), alternativa]}
        enrichers={enrichers}
        tables={tables}
        onChange={onChange}
      />,
    )

    fireEvent.click(screen.getByTestId("add-alternative-1"))

    const next = onChange.mock.calls[0][0]
    expect(next[2].when).toEqual({ lacks_tag: ["achou_no_cmdb", "achou_no_opencti"] })
    expect(new Set(next.map((r: EnrichmentRule) => r.id)).size).toBe(3)
  })
  it("não deixa encadear quando isso apagaria a condição existente", () => {
    // Um gate tem UMA chave (dsl.py:203-207). Somar `lacks_tag` a um `exists`
    // é impossível, e substituir faria a alternativa rodar justamente nos
    // eventos que o operador excluiu — com o compilador aceitando.
    render(
      <PolicyRuleEditor
        rules={[baseRule({ when: { exists: "normalized.src_endpoint.ip" } })]}
        enrichers={enrichers}
        tables={tables}
        onChange={vi.fn()}
      />,
    )
    expect(screen.getByTestId("add-alternative-0")).toBeDisabled()
  })

  it("grava o padrão com o tipo que o enricher devolve", () => {
    // `applier._write_outputs` escreve `out.default` CRU. String num campo que
    // o hit preenche com número faz o MESMO target ter dois tipos, e quebra em
    // quem consome só no evento de miss.
    const onChange = vi.fn()
    render(
      <PolicyRuleEditor
        rules={[
          baseRule({
            enricher: "virustotal",
            source: "vt",
            outputs: [{ from: "malicious_count", target: "_centralops.enrichment.vt.n" }],
          }),
        ]}
        enrichers={enrichers}
        tables={tables}
        onChange={onChange}
      />,
    )

    fireEvent.change(screen.getByLabelText("Padrão"), { target: { value: "0" } })
    expect(onChange.mock.calls[0][0][0].outputs[0].default).toBe(0)
  })

  it("volta on_miss para skip quando o último padrão é apagado", () => {
    const onChange = vi.fn()
    render(
      <PolicyRuleEditor
        rules={[
          baseRule({
            on_miss: "default",
            outputs: [
              { from: "site", target: "_centralops.enrichment.src.site", default: "desconhecido" },
            ],
          }),
        ]}
        enrichers={enrichers}
        tables={tables}
        onChange={onChange}
      />,
    )

    fireEvent.change(screen.getByLabelText("Padrão"), { target: { value: "" } })

    const next = onChange.mock.calls[0][0][0]
    expect(next.outputs[0].default).toBeUndefined()
    // Sem isto a regra publicava on_miss="default" sem nenhum default: a opção
    // fica desabilitada na tela mas o valor continua indo no payload.
    expect(next.on_miss).toBe("skip")
  })
})
