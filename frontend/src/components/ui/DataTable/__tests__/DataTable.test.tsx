import { render, screen } from "@testing-library/react"
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
