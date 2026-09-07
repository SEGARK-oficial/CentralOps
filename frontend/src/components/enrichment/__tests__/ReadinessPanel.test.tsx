/**
 * Painel de prontidão — a aba que responde "está funcionando aqui?".
 *
 * O que estes testes protegem é a diferença entre um painel que ORIENTA e uma
 * lista de alarmes. Em ordem:
 *
 * 1. O passo bloqueado leva a uma ação, e a ação sabe se o operador consegue
 *    executá-la (um admin de organização não abre a tela de instalação).
 * 2. O que não se aplica à organização aparece em silêncio, sem cor de alerta.
 * 3. Métricas e falhas recentes são complemento: se elas falharem, o painel
 *    ainda responde a pergunta principal.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { ReadinessPanel } from "@/components/enrichment/ReadinessPanel"
import * as api from "@/services/api"
import type { EnrichmentReadiness } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const navigate = vi.fn()
vi.mock("react-router-dom", async () => {
  const actual = await vi.importActual<typeof import("react-router-dom")>(
    "react-router-dom",
  )
  return { ...actual, useNavigate: () => navigate }
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const readyBase: EnrichmentReadiness = {
  organization_id: 1,
  ready: true,
  steps: [],
  active_policy_name: "contexto-de-ativo",
}

function mount(readiness: Partial<EnrichmentReadiness> = {}) {
  mockedApi.getEnrichmentReadiness.mockResolvedValue({ ...readyBase, ...readiness })
  return render(
    <MemoryRouter>
      <ReadinessPanel organizations={[{ id: 1, name: "Acme" }]} selectedOrgId={1} />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.getEnrichmentMetrics.mockResolvedValue({
    organization_id: 1,
    range_minutes: 60,
    policy_name: "contexto-de-ativo",
    rules: [],
  })
  mockedApi.getEnrichmentActivity.mockResolvedValue({
    organization_id: 1,
    entries: [],
  })
})

describe("ReadinessPanel", () => {
  it("diz que está pronto e nomeia a política que vale", async () => {
    mount()
    expect(await screen.findByText(/Tudo pronto nesta organização/i)).toBeInTheDocument()
    expect(screen.getByText(/contexto-de-ativo/)).toBeInTheDocument()
  })

  it("mostra o passo bloqueado com o motivo e a ação", async () => {
    mount({
      ready: false,
      steps: [
        {
          key: "cache_l2",
          status: "blocked",
          title: "Cache L2 dedicado (Redis)",
          detail: "Não configurado, e esta organização usa virustotal.",
          blocking: true,
          action: {
            label: "Configuração › Enriquecimento",
            route: "/config?tab=enrichment",
            scope: "global",
          },
        },
      ],
    })

    const step = await screen.findByTestId("readiness-step-cache_l2")
    expect(step).toHaveTextContent("virustotal")
    // Marca que o admin de organização não resolve sozinho: mandá-lo a uma
    // tela que ele não abre é pior do que não sugerir nada.
    expect(step).toHaveTextContent(/requer administrador global/i)
  })

  it("navega para outra página pelo router e para outra aba por callback", async () => {
    const onNavigateTab = vi.fn()
    mockedApi.getEnrichmentReadiness.mockResolvedValue({
      ...readyBase,
      ready: false,
      steps: [
        {
          key: "cache_l2",
          status: "blocked",
          title: "Cache",
          detail: "d",
          blocking: true,
          action: { label: "Ir para config", route: "/config?tab=enrichment", scope: "global" },
        },
        {
          key: "tables",
          status: "blocked",
          title: "Tabelas",
          detail: "d",
          blocking: true,
          action: { label: "Abrir tabelas", route: "/enrichment?tab=tables", scope: "org" },
        },
      ],
    })
    render(
      <MemoryRouter>
        <ReadinessPanel
          organizations={[{ id: 1, name: "Acme" }]}
          selectedOrgId={1}
          onNavigateTab={onNavigateTab}
        />
      </MemoryRouter>,
    )

    fireEvent.click(await screen.findByRole("button", { name: "Ir para config" }))
    expect(navigate).toHaveBeenCalledWith("/config?tab=enrichment")

    // Aba da MESMA página é troca local: recarregar o que já está em memória
    // seria desperdício e piscaria a tela.
    fireEvent.click(screen.getByRole("button", { name: "Abrir tabelas" }))
    expect(onNavigateTab).toHaveBeenCalledWith("tables")
    expect(navigate).toHaveBeenCalledTimes(1)
  })

  it("o que não se aplica à organização não vira alerta", async () => {
    mount({
      steps: [
        {
          key: "cache_l2",
          status: "not_applicable",
          title: "Cache L2 dedicado (Redis)",
          detail: "Nenhuma regra desta organização usa enricher por lote.",
          blocking: false,
          action: null,
        },
      ],
    })

    const step = await screen.findByTestId("readiness-step-cache_l2")
    // Sem ação: não há o que o operador faça, e oferecer um botão seria
    // convidá-lo a mexer no que não está quebrado.
    expect(step.querySelector("button")).toBeNull()
    expect(await screen.findByText(/Tudo pronto nesta organização/i)).toBeInTheDocument()
  })

  it("responde a pergunta principal mesmo com métricas e falhas indisponíveis", async () => {
    // Prontidão é a promessa da tela; o resto é complemento. Derrubar o painel
    // inteiro porque o contador não respondeu trocaria uma resposta parcial por
    // nenhuma resposta.
    mockedApi.getEnrichmentMetrics.mockRejectedValue(new Error("obs fora do ar"))
    mockedApi.getEnrichmentActivity.mockRejectedValue(new Error("obs fora do ar"))
    mount()

    expect(await screen.findByText(/Tudo pronto nesta organização/i)).toBeInTheDocument()
    expect(screen.queryByText(/obs fora do ar/i)).not.toBeInTheDocument()
  })

  it("mostra ErrorState quando a própria prontidão falha", async () => {
    mockedApi.getEnrichmentReadiness.mockRejectedValue(new Error("sem permissão"))
    render(
      <MemoryRouter>
        <ReadinessPanel organizations={[{ id: 1, name: "Acme" }]} selectedOrgId={1} />
      </MemoryRouter>,
    )
    expect(await screen.findByText(/sem permissão/i)).toBeInTheDocument()
  })

  it("consulta a organização do filtro global, não a primeira da lista", async () => {
    // Num MSP as duas divergem, e reportar a saúde de um tenant enquanto o
    // resto da tela fala de outro é o pior desfecho possível deste painel.
    mockedApi.getEnrichmentReadiness.mockResolvedValue(readyBase)
    render(
      <MemoryRouter>
        <ReadinessPanel
          organizations={[
            { id: 1, name: "Matriz" },
            { id: 7, name: "Filial" },
          ]}
          selectedOrgId={7}
        />
      </MemoryRouter>,
    )
    await waitFor(() =>
      expect(mockedApi.getEnrichmentReadiness).toHaveBeenCalledWith({
        organization_id: 7,
      }),
    )
  })
  it("mostra os números da janela e omite a fileira quando não há o que contar", async () => {
    // Uma fileira de zeros numa organização recém-configurada não informa
    // nada e rouba o lugar do passo que de fato falta.
    mount()
    await screen.findByText(/Tudo pronto nesta organização/i)
    expect(screen.queryByTestId("readiness-kpis")).not.toBeInTheDocument()
  })

  it("os KPIs vêm só das métricas, sem inventar contagem de eventos", async () => {
    mockedApi.getEnrichmentMetrics.mockResolvedValue({
      organization_id: 1,
      range_minutes: 60,
      policy_name: "p",
      rules: [
        { rule_id: "a", enricher: "table_cidr", source: null, hit: 90, miss: 10, skipped: 0, error: 0 },
        { rule_id: "b", enricher: "virustotal", source: "vt", hit: 0, miss: 0, skipped: 100, error: 0 },
        { rule_id: "c", enricher: "opencti", source: "cti", hit: 0, miss: 0, skipped: 0, error: 0 },
      ],
    })
    mount()

    const kpis = await screen.findByTestId("readiness-kpis")
    expect(kpis).toHaveTextContent("200")  // consultas na janela
    expect(kpis).toHaveTextContent("45%")  // acerto: 90 de 200
    expect(kpis).toHaveTextContent("50%")  // sem resposta: 100 de 200
    // A regra que não disparou é contada à parte: "0% de acerto" e "muda" são
    // diagnósticos diferentes, e confundi-los manda o operador mexer na
    // tabela quando o problema é a regra não estar rodando.
    expect(kpis).toHaveTextContent(/Regras mudas/i)
  })
})
