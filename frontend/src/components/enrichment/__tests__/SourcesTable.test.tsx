/**
 * Fontes como lista operacional.
 *
 * O defeito que esta tabela corrige: o grid de cards mostrava apenas se HAVIA
 * credencial, nunca se ela FUNCIONA. Uma fonte nunca testada, uma testada com
 * sucesso e uma com a chave rejeitada apareciam exatamente iguais, e descobrir
 * qual era qual exigia abrir uma a uma.
 */

import { render, screen, fireEvent, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import { SourcesTable } from "@/components/enrichment/SourcesTable"
import type { EnricherCatalogItem, EnrichmentSource } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const enrichers: EnricherCatalogItem[] = [
  {
    name: "virustotal",
    label: "VirusTotal",
    category: "Threat Intel",
    description: "Reputação de indicadores.",
    icon_id: null,
    docs_url: null,
    tier: "beta",
    order: 1,
    mode: "remote",
    key_kinds: ["ip"],
    supports_bulk: true,
    suggested_ttl_s: 3600,
    license: "core",
    egress: "third_party",
    required_secrets: ["api_key"],
    output_fields: {},
  },
  {
    name: "table_cidr",
    label: "Tabela CIDR",
    category: "Tabela do cliente",
    description: "Casa por prefixo.",
    icon_id: null,
    docs_url: null,
    tier: "beta",
    order: 0,
    mode: "local",
    key_kinds: ["ip"],
    supports_bulk: true,
    suggested_ttl_s: 3600,
    license: "core",
    egress: "none",
    required_secrets: [],
    output_fields: {},
  },
]

function src(over: Partial<EnrichmentSource> = {}): EnrichmentSource {
  return {
    id: "s1",
    organization_id: 1,
    name: "vt-prod",
    enricher: "virustotal",
    description: null,
    config: {},
    secret_configured: true,
    enabled: true,
    shared_organization_ids: [],
    last_test_at: null,
    last_test_ok: null,
    last_test_message: null,
    ...over,
  }
}

const noop = () => {}

function mount(sources: EnrichmentSource[], over: Record<string, unknown> = {}) {
  return render(
    <SourcesTable
      sources={sources}
      enrichers={enrichers}
      organizations={[
        { id: 1, name: "Matriz" },
        { id: 2, name: "Filial" },
      ]}
      onCreate={noop}
      onEdit={noop}
      onDelete={noop}
      onTest={noop}
      {...over}
    />,
  )
}

describe("SourcesTable", () => {
  it("distingue nunca testada, testada com sucesso e falhando", async () => {
    // A distinção é o ponto: as três pedem ações diferentes.
    mount([
      src({ id: "a", name: "nunca" }),
      src({
        id: "b",
        name: "ok",
        last_test_at: new Date(Date.now() - 60_000).toISOString(),
        last_test_ok: true,
      }),
      src({
        id: "c",
        name: "falhando",
        last_test_at: new Date(Date.now() - 60_000).toISOString(),
        last_test_ok: false,
        last_test_message: "401 Unauthorized: Your API key is invalid",
      }),
    ])

    expect(screen.getByText(/nunca testada/i)).toBeInTheDocument()
    // A mensagem do provedor aparece na própria linha: é o que permite agir sem
    // abrir log de worker.
    expect(screen.getByText(/401 Unauthorized/)).toBeInTheDocument()
  })

  it("mostra o selo de egresso sem exigir abrir a fonte", async () => {
    // Consentimento de privacidade não pode ficar escondido atrás de um clique.
    mount([src(), src({ id: "s2", name: "rede", enricher: "table_cidr" })])
    expect(screen.getByText("envia a terceiro")).toBeInTheDocument()
    expect(screen.getByText("sem egresso")).toBeInTheDocument()
  })

  it("filtra as que pedem atenção agora", async () => {
    mount([
      src({ id: "a", name: "saudavel", last_test_at: new Date().toISOString(), last_test_ok: true }),
      src({ id: "b", name: "sem-chave", secret_configured: false }),
      src({
        id: "c",
        name: "rejeitada",
        last_test_at: new Date().toISOString(),
        last_test_ok: false,
      }),
      src({ id: "d", name: "desligada", enabled: false }),
    ])

    // O Select do design system é um botão com listbox, não um <select>
    // nativo — interagir por `change` não dispara nada e o teste passaria a
    // medir a lista sem filtro.
    fireEvent.click(screen.getByLabelText(/Filtrar/i))
    fireEvent.click(await screen.findByRole("option", { name: /Com problema \(3\)/i }))

    expect(screen.queryByText("saudavel")).not.toBeInTheDocument()
    for (const nome of ["sem-chave", "rejeitada", "desligada"]) {
      expect(screen.getByText(nome)).toBeInTheDocument()
    }
  })

  it("não cobra credencial de enricher que não usa credencial", async () => {
    // Marcar "sem credencial" numa tabela do cliente seria um alerta falso, e
    // alerta falso ensina o operador a ignorar os verdadeiros.
    mount([src({ id: "t", name: "rede", enricher: "table_cidr", secret_configured: false })])
    expect(screen.getByText(/não exige/i)).toBeInTheDocument()
    expect(screen.queryByText("sem credencial")).not.toBeInTheDocument()
  })

  it("diz com quantas filhas a fonte é compartilhada", async () => {
    // Trocar a credencial de uma fonte compartilhada afeta os filhos; quem edita
    // precisa ver isso antes.
    mount([src({ shared_organization_ids: [2] })])
    expect(screen.getByText(/Matriz \+ 1 filha/i)).toBeInTheDocument()
  })

  it("dispara a sondagem da linha", async () => {
    const onTest = vi.fn()
    mount([src()], { onTest })

    const linha = screen.getByText("vt-prod").closest("tr")!
    fireEvent.click(within(linha).getByRole("button", { name: /Testar/i }))
    expect(onTest).toHaveBeenCalledWith(expect.objectContaining({ id: "s1" }))
  })

  it("não aceita um segundo clique enquanto a sondagem está em andamento", async () => {
    // Descobri isto escrevendo o teste anterior com `testingId` já preenchido:
    // o clique não chamava nada. É o comportamento certo — cada sondagem gasta
    // cota do provedor, e um duplo clique gastaria duas.
    const onTest = vi.fn()
    mount([src()], { onTest, testingId: "s1" })

    const linha = screen.getByText("vt-prod").closest("tr")!
    fireEvent.click(within(linha).getByRole("button", { name: /Testar/i }))
    expect(onTest).not.toHaveBeenCalled()
  })

  it("estado vazio convida a criar, em vez de mostrar tabela sem linha", async () => {
    const onCreate = vi.fn()
    mount([], { onCreate })
    expect(screen.getByText(/Nenhuma fonte configurada/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Nova fonte" }))
    expect(onCreate).toHaveBeenCalled()
  })
  it("permite criar fonte mesmo com a lista cheia", async () => {
    // Descoberto dirigindo a tela: ao fixar a ação primária do cabeçalho em
    // "Nova política", a aba de Fontes ficou sem por onde adicionar outra —
    // o botão só existia no estado vazio.
    const onCreate = vi.fn()
    mount([src()], { onCreate })
    fireEvent.click(screen.getByRole("button", { name: "Nova fonte" }))
    expect(onCreate).toHaveBeenCalled()
  })
})
