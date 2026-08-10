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
})
