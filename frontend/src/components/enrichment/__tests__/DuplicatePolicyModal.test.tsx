/**
 * Copiar política para outra organização.
 *
 * O que dá sentido a esta tela é o preflight. Sem ele a cópia nasceria válida
 * no destino e a busca da tabela falharia a cada ciclo — num log de worker que
 * ninguém lê, com os eventos saindo sem contexto e sem erro em tela nenhuma.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { DuplicatePolicyModal } from "@/components/enrichment/DuplicatePolicyModal"
import * as api from "@/services/api"
import type { EnrichmentPolicy } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const policy: EnrichmentPolicy = {
  id: "p1",
  organization_id: 1,
  name: "padrao-soc",
  description: null,
  enabled: true,
  current_version_id: "v1",
  rule_count: 2,
  is_active: true,
}

const orgs = [
  { id: 1, name: "Matriz" },
  { id: 2, name: "Filial" },
]

function mount(onDuplicated = vi.fn()) {
  render(
    <DuplicatePolicyModal
      open
      policy={policy}
      organizations={orgs}
      onClose={vi.fn()}
      onDuplicated={onDuplicated}
    />,
  )
  return onDuplicated
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("DuplicatePolicyModal", () => {
  it("verifica o destino ao abrir, sem esperar o clique em copiar", async () => {
    // Esperar o clique transformaria a verificação num erro, quando ela é uma
    // orientação: o operador precisa saber o que criar no destino ANTES.
    mockedApi.preflightDuplicateEnrichmentPolicy.mockResolvedValue({
      target_organization_id: 2,
      ok: true,
      missing_tables: [],
      missing_sources: [],
      tables_without_version: [],
      name_conflict: false,
    })
    mount()

    await waitFor(() =>
      expect(mockedApi.preflightDuplicateEnrichmentPolicy).toHaveBeenCalled(),
    )
    expect(await screen.findByText(/O destino tem tudo/i)).toBeInTheDocument()
    expect(mockedApi.duplicateEnrichmentPolicy).not.toHaveBeenCalled()
  })

  it("bloqueia e NOMEIA o que falta no destino", async () => {
    mockedApi.preflightDuplicateEnrichmentPolicy.mockResolvedValue({
      target_organization_id: 2,
      ok: false,
      missing_tables: ["plano-de-rede"],
      missing_sources: ["vt-prod"],
      tables_without_version: [],
      name_conflict: false,
    })
    mount()

    const pre = await screen.findByTestId("duplicate-preflight")
    expect(pre).toHaveTextContent("plano-de-rede")
    expect(pre).toHaveTextContent("vt-prod")
    expect(screen.getByRole("button", { name: "Copiar" })).toBeDisabled()
  })

  it("tabela sem versão no destino avisa mas não impede a cópia", async () => {
    // Existir e estar publicada são coisas diferentes: barrar aqui obrigaria a
    // ordem inversa — publicar a tabela antes de saber quais campos a política
    // usa.
    mockedApi.preflightDuplicateEnrichmentPolicy.mockResolvedValue({
      target_organization_id: 2,
      ok: true,
      missing_tables: [],
      missing_sources: [],
      tables_without_version: ["plano-de-rede"],
      name_conflict: false,
    })
    mount()

    expect(await screen.findByText(/sem versão publicada no destino/i)).toBeInTheDocument()
    expect(screen.getByRole("button", { name: "Copiar" })).not.toBeDisabled()
  })

  it("diz que a cópia nasce inativa", async () => {
    // É a expectativa que mais gera surpresa: copiar não coloca regra em
    // produção no tenant vizinho.
    mockedApi.preflightDuplicateEnrichmentPolicy.mockResolvedValue({
      target_organization_id: 2,
      ok: true,
      missing_tables: [],
      missing_sources: [],
      tables_without_version: [],
      name_conflict: false,
    })
    mount()
    expect(await screen.findByText(/cópia nasce inativa/i)).toBeInTheDocument()
  })

  it("copia e devolve a política criada", async () => {
    mockedApi.preflightDuplicateEnrichmentPolicy.mockResolvedValue({
      target_organization_id: 2,
      ok: true,
      missing_tables: [],
      missing_sources: [],
      tables_without_version: [],
      name_conflict: false,
    })
    const criada = { ...policy, id: "p2", organization_id: 2, enabled: false }
    mockedApi.duplicateEnrichmentPolicy.mockResolvedValue(criada)
    const onDuplicated = mount()

    await screen.findByText(/O destino tem tudo/i)
    fireEvent.click(screen.getByRole("button", { name: "Copiar" }))

    await waitFor(() => expect(onDuplicated).toHaveBeenCalledWith(criada))
    expect(mockedApi.duplicateEnrichmentPolicy.mock.calls[0][1].target_organization_id).toBe(2)
  })
})
