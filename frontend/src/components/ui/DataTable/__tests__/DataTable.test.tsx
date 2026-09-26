import { render, screen, fireEvent } from "@testing-library/react"
import { DataTable } from "@/components/ui/DataTable/DataTable"
import type { TableColumn } from "@/types"

interface Row extends Record<string, unknown> {
  id: number
  name: string
}

const columns: TableColumn<Row>[] = [
  { key: "id", title: "ID", dataIndex: "id" },
  { key: "name", title: "Nome", dataIndex: "name" },
]

function buildRows(count: number): Row[] {
  return Array.from({ length: count }, (_, i) => ({ id: i + 1, name: `Item ${i + 1}` }))
}

// Jsdom não tem layout real — o virtualizer retorna 0 itens por padrão.
// Mock para simular o comportamento de virtualização em ambiente de teste.
vi.mock("@tanstack/react-virtual", () => ({
  useVirtualizer: ({ count }: { count: number }) => ({
    getVirtualItems: () =>
      Array.from({ length: Math.min(count, 10) }, (_, i) => ({
        key: i,
        index: i,
        start: i * 48,
        measureElement: vi.fn(),
      })),
    getTotalSize: () => count * 48,
    measureElement: vi.fn(),
  }),
}))

describe("DataTable — sem virtualização (comportamento atual)", () => {
  it("renderiza todos os rows no DOM quando virtualizeRows não é passado", () => {
    const rows = buildRows(10)
    render(<DataTable data={rows} columns={columns} />)
    // Todos os 10 rows devem estar no DOM (+1 pelo cabeçalho)
    const trs = screen.getAllByRole("row")
    expect(trs.length).toBe(11)
  })

  it("preserva API retrocompatível (sem novas props obrigatórias)", () => {
    expect(() =>
      render(<DataTable data={[]} columns={columns} />),
    ).not.toThrow()
  })

  it("exibe mensagem de vazio quando data=[]", () => {
    render(<DataTable data={[]} columns={columns} emptyMessage="Sem resultados" />)
    expect(screen.getByText("Sem resultados")).toBeInTheDocument()
  })
})

describe("DataTable — loading (LAY-10: skeleton em vez de spinner de página inteira)", () => {
  it("mostra skeleton com a forma da tabela (cabeçalho real), não um spinner que colapsa a área", () => {
    render(<DataTable data={[]} columns={columns} loading />)
    // O cabeçalho de verdade continua na tela — é o que reserva a largura das
    // colunas e evita o salto de layout (CLS) quando os dados chegam.
    expect(screen.getByText("ID")).toBeInTheDocument()
    expect(screen.getByText("Nome")).toBeInTheDocument()
  })

  it("continua anunciando 'carregando' para leitor de tela via role=status", () => {
    // Mesmo contrato de acessibilidade do LoadingSpinner que este skeleton
    // substitui — só que como uma região sr-only à parte, não mais como o
    // wrapper visual inteiro.
    render(<DataTable data={[]} columns={columns} loading />)
    const status = screen.getByRole("status")
    expect(status).toHaveAttribute("aria-busy", "true")
    expect(status).toHaveTextContent(/carregando/i)
  })
})

describe("DataTable — serverSide pagination", () => {
  it("quando serverSide=true, renderiza todos os items recebidos sem slice", () => {
    // Simula page 2: backend entregou apenas os itens 21-25 (5 itens).
    // Com paginação client-side o slice seria (2-1)*20 = 20..40 em 5 itens → vazio.
    // Com serverSide=true os 5 itens devem aparecer.
    const rows = buildRows(5)
    render(
      <DataTable
        data={rows}
        columns={columns}
        pagination={{ current: 2, pageSize: 20, total: 45 }}
        serverSide
      />,
    )
    // 5 rows de dados + 1 row de cabeçalho
    const trs = screen.getAllByRole("row")
    expect(trs.length).toBe(6)
    expect(screen.getByText("Item 5")).toBeInTheDocument()
  })

  it("sem serverSide (default false), page 2 com 5 items exibe só o cabeçalho (sem rows de dados)", () => {
    // Este teste documenta o comportamento ANTERIOR (agora protegido pela prop)
    // para garantir que o default não mudou silenciosamente.
    // Com 5 itens e pageSize=20, currentPage=2: start=20, slice(20,40) de arr[5] = []
    // O DataTable renderiza a tabela porque data.length > 0, mas tbody fica vazio.
    const rows = buildRows(5)
    render(
      <DataTable
        data={rows}
        columns={columns}
        pagination={{ current: 2, pageSize: 20, total: 45 }}
        emptyMessage="Sem resultados"
      />,
    )
    // Apenas o row de cabeçalho aparece — nenhum row de dado é renderizado
    const trs = screen.getAllByRole("row")
    expect(trs.length).toBe(1)
    expect(screen.queryByText("Item 5")).not.toBeInTheDocument()
  })

  it("backwards-compat: serverSide=false (omitido) faz slice client-side em page 1", () => {
    const rows = buildRows(25)
    render(
      <DataTable
        data={rows}
        columns={columns}
        pagination={{ current: 1, pageSize: 10, total: 25 }}
      />,
    )
    // Page 1 com pageSize=10: deve mostrar 10 rows + cabeçalho
    const trs = screen.getAllByRole("row")
    expect(trs.length).toBe(11)
  })
})

describe("DataTable — tableClassName (LAY-22)", () => {
  it("aplica a classe só no wrapper da tabela, não na paginação", () => {
    const rows = buildRows(25)
    render(
      <DataTable
        data={rows}
        columns={columns}
        tableClassName="min-w-[760px]"
        pagination={{ current: 1, pageSize: 10, total: 25, showTotal: true }}
      />,
    )
    const table = screen.getByRole("table")
    const tableWrapper = table.parentElement
    expect(tableWrapper).toHaveClass("min-w-[760px]")

    // A paginação (texto "a ... de ...") não deve estar dentro do wrapper
    // que carrega a largura mínima — senão ela também "empurraria" a
    // paginação para fora da viewport em telas estreitas.
    const paginationText = screen.getByText(/1 a 10 de 25/)
    expect(tableWrapper?.contains(paginationText)).toBe(false)
  })
})

describe("DataTable — com virtualização", () => {
  it("renderiza sem crashar com 1000 rows e virtualizeRows=true", () => {
    const rows = buildRows(1000)
    expect(() =>
      render(
        <DataTable
          data={rows}
          columns={columns}
          virtualizeRows={true}
          maxHeight="400px"
        />,
      ),
    ).not.toThrow()
  })

  it("com virtualizeRows=true, DOM tem muito menos que 1000 <tr> de dados", () => {
    const rows = buildRows(1000)
    render(
      <DataTable
        data={rows}
        columns={columns}
        virtualizeRows={true}
        maxHeight="400px"
      />,
    )
    // O mock retorna no máximo 10 rows virtualizados + 1 header
    const trs = screen.getAllByRole("row")
    expect(trs.length).toBeLessThan(50)
    expect(trs.length).toBeGreaterThan(1)
  })

  it("aceita maxHeight como string", () => {
    expect(() =>
      render(
        <DataTable
          data={buildRows(5)}
          columns={columns}
          virtualizeRows={true}
          maxHeight="300px"
        />,
      ),
    ).not.toThrow()
  })
})

describe("DataTable — ordena por dataIndex, não por key (BUG-02)", () => {
  interface SizedRow extends Record<string, unknown> {
    id: number
    approx_bytes: number
  }
  // Mesmo formato de TablesTable.tsx: rótulo da coluna ("size") ≠ campo do
  // registro ("approx_bytes"). Ordenar por `key` comparava `undefined` com
  // `undefined` em toda linha (a coluna nunca reordenava de verdade).
  const sizedColumns: TableColumn<SizedRow>[] = [
    { key: "id", title: "ID", dataIndex: "id" },
    { key: "size", title: "Tamanho", dataIndex: "approx_bytes", sortable: true },
  ]
  const sizedRows: SizedRow[] = [
    { id: 1, approx_bytes: 300 },
    { id: 2, approx_bytes: 100 },
    { id: 3, approx_bytes: 200 },
  ]

  it("clicar no cabeçalho 'Tamanho' ordena pelos valores de approx_bytes", () => {
    render(<DataTable data={sizedRows} columns={sizedColumns} />)
    const header = screen.getByRole("button", { name: /Tamanho/ })
    fireEvent.click(header)

    const cells = screen.getAllByRole("row").slice(1).map((row) => row.textContent)
    // Ascendente por approx_bytes: 100 (id 2), 200 (id 3), 300 (id 1).
    expect(cells).toEqual(["2100", "3200", "1300"])
  })
})

describe("DataTable — serverSide não ordena localmente (PERF-12)", () => {
  it("clicar num cabeçalho sortable com serverSide=true não reordena a página local", () => {
    const rows = buildRows(5)
    render(
      <DataTable
        data={[...rows].reverse()}
        columns={[{ key: "id", title: "ID", dataIndex: "id", sortable: true }, columns[1]]}
        serverSide
      />,
    )
    const before = screen.getAllByRole("row").slice(1).map((r) => r.textContent)
    fireEvent.click(screen.getByRole("button", { name: /ID/ }))
    const after = screen.getAllByRole("row").slice(1).map((r) => r.textContent)
    // Mesma ordem de antes: ordenar só a página corrente, sem o resto do
    // dataset, produziria uma ordenação FALSA (a página 2 nunca fica coerente
    // com a 1). Quem ordena de fato é o backend, via onSortChange.
    expect(after).toEqual(before)
  })

  it("emite onSortChange com a coluna (dataIndex) e a direção", () => {
    const onSortChange = vi.fn()
    const sortableColumns: TableColumn<Row>[] = [columns[0], { ...columns[1], sortable: true }]
    render(
      <DataTable
        data={buildRows(3)}
        columns={sortableColumns}
        serverSide
        onSortChange={onSortChange}
      />,
    )
    fireEvent.click(screen.getByRole("button", { name: /Nome/ }))
    expect(onSortChange).toHaveBeenCalledWith("name", "asc")
    fireEvent.click(screen.getByRole("button", { name: /Nome/ }))
    expect(onSortChange).toHaveBeenCalledWith("name", "desc")
  })
})

describe("DataTable — aria-sort só em coluna sortable", () => {
  it("omite aria-sort em coluna não ordenável, e marca 'none' na ordenável", () => {
    render(<DataTable data={buildRows(3)} columns={[{ ...columns[0], sortable: true }, columns[1]]} />)
    const headers = screen.getAllByRole("columnheader")
    expect(headers[0]).toHaveAttribute("aria-sort", "none")
    expect(headers[1]).not.toHaveAttribute("aria-sort")
  })
})

describe("DataTable — rowKey (BUG-05)", () => {
  it("usa o campo indicado por rowKey como key de cada linha, em vez do índice", () => {
    const rows = buildRows(3)
    expect(() =>
      render(<DataTable data={rows} columns={columns} rowKey="id" />),
    ).not.toThrow()
    // As 3 linhas de dado renderizam normalmente — a troca de key não afeta o conteúdo.
    expect(screen.getByText("Item 1")).toBeInTheDocument()
    expect(screen.getByText("Item 3")).toBeInTheDocument()
  })

  it("aceita uma função rowKey(record, index)", () => {
    const rows = buildRows(3)
    expect(() =>
      render(<DataTable data={rows} columns={columns} rowKey={(r) => `row-${r.id}`} />),
    ).not.toThrow()
    expect(screen.getByText("Item 2")).toBeInTheDocument()
  })
})
