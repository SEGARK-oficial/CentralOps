/**
 * Tabelas do cliente como lista.
 *
 * O card mostrava nome, contagem e tamanho, e escondia as duas coisas que
 * decidem uma ação: se há versão publicada (sem ela a busca falha a cada ciclo,
 * sem erro em tela) e se alguma regra cita a tabela (sem isso ela é peso morto;
 * com isso ela não pode ser apagada).
 */

import { render, screen, fireEvent, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import { TablesTable } from "@/components/enrichment/TablesTable"
import type { EnrichmentTable } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

function tbl(over: Partial<EnrichmentTable> = {}): EnrichmentTable {
  return {
    id: "t1",
    organization_id: 1,
    name: "plano-de-rede",
    description: null,
    match_mode: "cidr",
    key_kind: "ip",
    current_version_id: "v3",
    entry_count: 1204,
    approx_bytes: 90112,
    ...over,
  }
}

const noop = () => {}

function mount(tables: EnrichmentTable[], over: Record<string, unknown> = {}) {
  render(
    <TablesTable
      tables={tables}
      onCreate={noop}
      onOpen={noop}
      onDelete={noop}
      {...over}
    />,
  )
}

describe("TablesTable", () => {
  it("destaca a tabela sem versão publicada", async () => {
    // É o caso de suporte nº 2: a regra cita a tabela, a carga falha a cada
    // ciclo, e o evento sai sem contexto sem nenhum erro na tela.
    mount([
      tbl(),
      tbl({ id: "t2", name: "allowlist", current_version_id: null, entry_count: 0 }),
    ])
    expect(screen.getByText(/sem versão publicada/i)).toBeInTheDocument()
  })

  it("diz quantas regras citam a tabela, e quando nenhuma cita", async () => {
    mount([tbl(), tbl({ id: "t2", name: "orfa" })], {
      citedBy: { "plano-de-rede": ["regra-site", "regra-criticidade"] },
    })

    const usada = screen.getByText("plano-de-rede").closest("tr")!
    expect(within(usada).getByText(/2 regras/i)).toBeInTheDocument()

    // Não é erro: uma tabela pode existir antes da regra que vai usá-la.
    const orfa = screen.getByText("orfa").closest("tr")!
    expect(within(orfa).getByText(/nenhuma regra/i)).toBeInTheDocument()
  })

  it("mede o tamanho contra o teto que vem da configuração", async () => {
    // "88 KiB" sozinho não diz se sobra espaço, e estourar o teto é recusa no
    // servidor. O teto é editável no console, então não pode ser fixo aqui.
    const { container } = render(
      <TablesTable
        tables={[tbl({ approx_bytes: 8 * 1024 * 1024 })]}
        maxTableBytes={10 * 1024 * 1024}
        onCreate={noop}
        onOpen={noop}
        onDelete={noop}
      />,
    )
    expect(screen.getByText("8.0 MiB")).toBeInTheDocument()
    // 80% do teto: a barra passa a avisar.
    const barra = container.querySelector('[style*="width"]') as HTMLElement
    expect(barra.className).toMatch(/warning/)
  })

  it("abrir e apagar são ações distintas da mesma linha", async () => {
    const onOpen = vi.fn()
    const onDelete = vi.fn()
    mount([tbl()], { onOpen, onDelete })

    fireEvent.click(screen.getByTestId("table-row-plano-de-rede"))
    expect(onOpen).toHaveBeenCalled()

    const linha = screen.getByText("plano-de-rede").closest("tr")!
    fireEvent.click(within(linha).getByRole("button", { name: "Apagar tabela" }))
    expect(onDelete).toHaveBeenCalled()
  })

  it("estado vazio convida a criar", async () => {
    const onCreate = vi.fn()
    mount([], { onCreate })
    expect(screen.getByText(/Nenhuma tabela ainda/i)).toBeInTheDocument()
    fireEvent.click(screen.getByRole("button", { name: "Nova tabela" }))
    expect(onCreate).toHaveBeenCalled()
  })
})
