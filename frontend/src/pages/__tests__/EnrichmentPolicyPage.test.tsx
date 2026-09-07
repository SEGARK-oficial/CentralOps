/**
 * Editor de política em página própria.
 *
 * O foco destes testes é o RASCUNHO, que é a parte com mais formas de dar
 * errado em silêncio:
 *
 * - Um rascunho feito sobre a v2 e restaurado por cima da v3 apagaria o que a
 *   v3 trouxe, publicando com 201 e sem nenhum aviso.
 * - Um rascunho que sobrevive à publicação faria a página anunciar alterações
 *   pendentes segundos depois de publicá-las.
 * - Um rascunho gravado quando nada mudou faria toda abertura da página
 *   anunciar alterações que não existem.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter, Route, Routes } from "react-router-dom"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import EnrichmentPolicyPage from "@/pages/EnrichmentPolicyPage"
import * as api from "@/services/api"
import type { EnrichmentPolicy, EnrichmentRule } from "@/services/api"
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

const POLICY_ID = "p1"
const DRAFT_KEY = `centralops:enrich:policy-draft:${POLICY_ID}`

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

function policy(over: Partial<EnrichmentPolicy> = {}): EnrichmentPolicy {
  return {
    id: POLICY_ID,
    organization_id: 1,
    name: "contexto-de-ativo",
    description: null,
    enabled: true,
    current_version_id: "v3",
    rule_count: 1,
    is_active: true,
    ...over,
  }
}

function mount() {
  return render(
    <MemoryRouter initialEntries={[`/enrichment/policies/${POLICY_ID}`]}>
      <Routes>
        <Route path="/enrichment/policies/:id" element={<EnrichmentPolicyPage />} />
      </Routes>
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  window.localStorage.clear()
  mockedApi.listEnrichmentPolicies.mockResolvedValue([policy()])
  mockedApi.listEnrichers.mockResolvedValue([])
  mockedApi.listEnrichmentTables.mockResolvedValue([])
  mockedApi.listEnrichmentSources.mockResolvedValue([])
  mockedApi.listMappings.mockResolvedValue([])
  mockedApi.listEnrichmentPolicyVersions.mockResolvedValue([
    {
      id: "v3",
      version_number: 3,
      commit_message: "vigente",
      author_user_id: 1,
      created_at: "2026-09-01T00:00:00Z",
      is_current: true,
      summary: { rule_count: 1 },
    },
  ] as never)
  mockedApi.getEnrichmentPolicyVersion.mockResolvedValue({
    id: "v3",
    version_number: 3,
    rules: [rule("regra-site")],
  } as never)
  mockedApi.listEnrichmentKeySources.mockResolvedValue({
    organization_id: 1,
    from_active_mappings: true,
    suggestions: [{ path: "normalized.src_endpoint.ip" }],
  } as never)
})

describe("EnrichmentPolicyPage", () => {
  it("abre com as regras da versão vigente e sem alterações pendentes", async () => {
    mount()
    await screen.findByText("contexto-de-ativo")

    // O editor abre hidratado porque publicar substitui a lista inteira: abrir
    // vazio publicaria uma versão com uma regra só.
    expect(await screen.findByTestId("policy-rule-editor")).toBeInTheDocument()
    expect(screen.queryByTestId("unpublished-badge")).not.toBeInTheDocument()
    expect(screen.getByTestId("policy-diff-empty")).toBeInTheDocument()
  })

  it("não grava rascunho quando nada foi alterado", async () => {
    // Gravar um rascunho idêntico ao publicado faria toda abertura da página
    // anunciar alterações que não existem.
    mount()
    await screen.findByTestId("policy-rule-editor")
    await waitFor(() => {
      expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull()
    })
  })

  it("grava o rascunho ao editar e o anuncia no cabeçalho", async () => {
    mount()
    await screen.findByTestId("policy-rule-editor")

    fireEvent.click(screen.getByTestId("add-rule"))

    expect(await screen.findByTestId("unpublished-badge")).toBeInTheDocument()
    await waitFor(() => {
      expect(window.localStorage.getItem(DRAFT_KEY)).not.toBeNull()
    })
    const saved = JSON.parse(window.localStorage.getItem(DRAFT_KEY)!)
    expect(saved.baseVersionId).toBe("v3")
    expect(saved.rules).toHaveLength(2)
  })

  it("restaura o rascunho e AVISA que não é o que está valendo", async () => {
    window.localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({
        rules: [rule("regra-site"), rule("rascunho")],
        baseVersionId: "v3",
        savedAt: Date.now(),
      }),
    )
    mount()

    // Sem o aviso o operador acha que está vendo produção e publica um
    // rascunho que já tinha esquecido.
    expect(
      await screen.findByText(/tinha alterações não publicadas aqui/i),
    ).toBeInTheDocument()
    expect(screen.getByTestId("unpublished-badge")).toBeInTheDocument()
  })

  it("descarta rascunho feito sobre uma versão que não é mais a vigente", async () => {
    // É o caso perigoso: aplicar por cima da v3 um rascunho da v2 apagaria o
    // que a v3 trouxe, publicando com 201 e sem aviso nenhum.
    window.localStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({
        rules: [rule("antiga")],
        baseVersionId: "v2",
        savedAt: Date.now(),
      }),
    )
    mount()
    await screen.findByTestId("policy-rule-editor")

    expect(screen.queryByText(/tinha alterações não publicadas/i)).not.toBeInTheDocument()
    await waitFor(() => {
      expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull()
    })
  })

  it("publicar limpa o rascunho e zera o diff", async () => {
    mockedApi.commitEnrichmentPolicyVersion.mockResolvedValue({
      id: "v4",
      version_number: 4,
      summary: { rule_count: 2 },
    } as never)
    mount()
    await screen.findByTestId("policy-rule-editor")

    fireEvent.click(screen.getByTestId("add-rule"))
    await screen.findByTestId("unpublished-badge")

    fireEvent.change(screen.getByLabelText(/Mensagem do commit/i), {
      target: { value: "adiciona regra" },
    })
    fireEvent.click(screen.getByRole("button", { name: /Publicar v/i }))

    await waitFor(() => {
      expect(mockedApi.commitEnrichmentPolicyVersion).toHaveBeenCalled()
    })
    // Manter o rascunho faria a página anunciar alterações pendentes segundos
    // depois de publicá-las.
    //
    // Este teste prova a PROPRIEDADE, não uma linha: ela é sustentada por dois
    // mecanismos (a limpeza explícita no fluxo de publicação e o efeito que
    // apaga o rascunho quando o editor volta a ficar igual ao publicado).
    // Mutar qualquer um dos dois isoladamente mantém o teste verde, e isso é o
    // resultado esperado de uma defesa em profundidade — não um teste fraco.
    await waitFor(() => {
      expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull()
    })
    expect(await screen.findByTestId("policy-diff-empty")).toBeInTheDocument()
  })

  it("descartar alterações volta para a versão vigente", async () => {
    mount()
    await screen.findByTestId("policy-rule-editor")

    fireEvent.click(screen.getByTestId("add-rule"))
    await screen.findByTestId("unpublished-badge")

    fireEvent.click(screen.getByRole("button", { name: /Descartar alterações/i }))

    await waitFor(() => {
      expect(screen.queryByTestId("unpublished-badge")).not.toBeInTheDocument()
    })
    expect(window.localStorage.getItem(DRAFT_KEY)).toBeNull()
  })

  it("exige mensagem de commit antes de publicar", async () => {
    mount()
    await screen.findByTestId("policy-rule-editor")
    fireEvent.click(screen.getByTestId("add-rule"))

    fireEvent.click(screen.getByRole("button", { name: /Publicar v/i }))

    expect(await screen.findByText(/Descreva a mudança antes de publicar/i)).toBeInTheDocument()
    expect(mockedApi.commitEnrichmentPolicyVersion).not.toHaveBeenCalled()
  })

  it("política inexistente mostra erro em vez de tela vazia", async () => {
    mockedApi.listEnrichmentPolicies.mockResolvedValue([])
    mount()
    expect(await screen.findByText(/Política não encontrada/i)).toBeInTheDocument()
  })
})
