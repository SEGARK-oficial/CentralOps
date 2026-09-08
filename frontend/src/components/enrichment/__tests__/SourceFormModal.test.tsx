/**
 * Cadastro de fonte: testar antes de salvar e consentir o egresso.
 *
 * Duas propriedades, e as duas existem porque o custo do erro é assimétrico:
 *
 * 1. **Testar antes de salvar.** Sem isso, o único jeito de descobrir que a
 *    chave está errada era gravar, esperar o ciclo e ler a aba de Execução —
 *    o operador só sabia que errou depois de a credencial já estar no banco e
 *    a política já publicada.
 * 2. **Consentimento explícito de egresso.** O selo do catálogo *informa* que a
 *    fonte manda indicador para fora; cadastrar a credencial é o que de fato
 *    *faz* isso acontecer. Em ambiente regulado, essa decisão precisa ser
 *    deliberada, não efeito colateral de preencher um formulário.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { SourceFormModal } from "@/components/enrichment/SourceFormModal"
import * as api from "@/services/api"
import type { EnricherCatalogItem, EnrichmentSource } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/contexts/PlatformContext", () => ({
  usePlatform: () => ({
    organizations: [{ id: 1, name: "Acme" }],
    selectedOrgId: 1,
  }),
}))

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

function enricher(over: Partial<EnricherCatalogItem> = {}): EnricherCatalogItem {
  return {
    name: "virustotal",
    label: "VirusTotal",
    category: "Threat Intel",
    description: "Reputação.",
    icon_id: null,
    docs_url: null,
    tier: "beta",
    order: 0,
    mode: "remote",
    key_kinds: ["ip", "file_hash"],
    supports_bulk: true,
    suggested_ttl_s: 3600,
    license: "core",
    egress: "third_party",
    required_secrets: ["api_key"],
    output_fields: {},
    ...over,
  } as EnricherCatalogItem
}

const semEgresso = enricher({
  name: "table_cidr",
  label: "Tabela CIDR",
  egress: "none",
  required_secrets: [],
  mode: "local",
})

const fonteSalva: EnrichmentSource = {
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
}

function mount(source: EnrichmentSource | null = null) {
  const onSaved = vi.fn()
  render(
    <SourceFormModal
      open
      source={source}
      enrichers={[enricher(), semEgresso]}
      organizations={[{ id: 1, name: "Acme" }]}
      onClose={vi.fn()}
      onSaved={onSaved}
    />,
  )
  return onSaved
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("SourceFormModal", () => {
  it("testa com a credencial digitada, sem gravar a fonte", async () => {
    mockedApi.testEnrichmentSourceDraft.mockResolvedValue({
      ok: true,
      message: "Credencial aceita pelo provedor.",
    })
    mount()

    fireEvent.change(screen.getByLabelText(/^Credencial/i), {
      target: { value: "chave-nova" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Testar" }))

    await waitFor(() =>
      expect(mockedApi.testEnrichmentSourceDraft).toHaveBeenCalled(),
    )
    expect(mockedApi.testEnrichmentSourceDraft.mock.calls[0][0].secret).toBe(
      "chave-nova",
    )
    expect(await screen.findByText(/Credencial aceita/i)).toBeInTheDocument()
    // Testar não cria fonte nenhuma.
    expect(mockedApi.createEnrichmentSource).not.toHaveBeenCalled()
  })

  it("na edição sem mexer na credencial, sonda a fonte GRAVADA", async () => {
    // É a sondagem mais fiel: usa exatamente o que o worker usaria.
    mockedApi.testEnrichmentSource.mockResolvedValue({ ok: true, message: "Conexão ok." })
    mount(fonteSalva)

    fireEvent.click(screen.getByRole("button", { name: "Testar" }))

    await waitFor(() => expect(mockedApi.testEnrichmentSource).toHaveBeenCalledWith("s1"))
    expect(mockedApi.testEnrichmentSourceDraft).not.toHaveBeenCalled()
  })

  it("na edição COM credencial nova, sonda o rascunho e cita a fonte", async () => {
    mockedApi.testEnrichmentSourceDraft.mockResolvedValue({ ok: true, message: "ok" })
    mount(fonteSalva)

    fireEvent.change(screen.getByLabelText(/^Credencial/i), {
      target: { value: "chave-rotacionada" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Testar" }))

    await waitFor(() =>
      expect(mockedApi.testEnrichmentSourceDraft).toHaveBeenCalled(),
    )
    const payload = mockedApi.testEnrichmentSourceDraft.mock.calls[0][0]
    expect(payload.secret).toBe("chave-rotacionada")
    expect(payload.source_id).toBe("s1")
    // A fonte gravada NÃO é sondada: ela ainda tem a credencial antiga.
    expect(mockedApi.testEnrichmentSource).not.toHaveBeenCalled()
  })

  it("não salva fonte com egresso a terceiro sem consentimento", async () => {
    mount()

    fireEvent.change(screen.getByLabelText(/^Nome/i), { target: { value: "vt-prod" } })
    fireEvent.change(screen.getByLabelText(/^Credencial/i), {
      target: { value: "chave" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Nova fonte" }))

    expect(
      await screen.findByText(/Confirme o envio de indicadores a terceiro/i),
    ).toBeInTheDocument()
    expect(mockedApi.createEnrichmentSource).not.toHaveBeenCalled()
  })

  it("salva depois do consentimento, e diz quais indicadores saem", async () => {
    mockedApi.createEnrichmentSource.mockResolvedValue(fonteSalva)
    const onSaved = mount()

    // A consequência é nomeada: não "egresso", mas quais tipos de indicador.
    expect(screen.getByTestId("egress-ack")).toHaveTextContent(/ip, file_hash/)

    fireEvent.change(screen.getByLabelText(/^Nome/i), { target: { value: "vt-prod" } })
    fireEvent.change(screen.getByLabelText(/^Credencial/i), {
      target: { value: "chave" },
    })
    fireEvent.click(screen.getByTestId("egress-ack").querySelector("input")!)
    fireEvent.click(screen.getByRole("button", { name: "Nova fonte" }))

    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it("enricher sem egresso não pede consentimento nenhum", async () => {
    // Pedir autorização para uma tabela local seria ruído, e ruído treina o
    // operador a marcar caixas sem ler.
    mockedApi.createEnrichmentSource.mockResolvedValue({
      ...fonteSalva,
      enricher: "table_cidr",
    })
    const onSaved = mount()

    // O Select do design system é botão + listbox, não `<select>` nativo:
    // `fireEvent.change` não dispara nada e o teste passaria medindo o
    // enricher anterior.
    fireEvent.click(screen.getByLabelText(/^Enricher/i))
    fireEvent.click(await screen.findByRole("option", { name: /Tabela CIDR/i }))
    await waitFor(() => {
      expect(screen.queryByTestId("egress-ack")).not.toBeInTheDocument()
    })

    fireEvent.change(screen.getByLabelText(/^Nome/i), { target: { value: "rede" } })
    fireEvent.click(screen.getByRole("button", { name: "Nova fonte" }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })

  it("editar fonte existente não reapresenta o consentimento em branco", async () => {
    // O consentimento foi dado na criação. Reapresentá-lo vazio faria toda
    // edição de porta parecer uma autorização nova.
    mockedApi.updateEnrichmentSource.mockResolvedValue(fonteSalva)
    const onSaved = mount(fonteSalva)

    expect(screen.getByTestId("egress-ack").querySelector("input")).toBeChecked()
    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))
    await waitFor(() => expect(onSaved).toHaveBeenCalled())
  })
})
