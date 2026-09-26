/**
 * R3-8.1 (regressão da Rodada 2): `IntegrationDetailPage` tem abas cujo
 * conteúdo vem de outro namespace de i18n (`dashboard`/`config`). Sem
 * `<Suspense>` local em `TabsPanel`, o componente da aba suspende até o
 * único `<Suspense>` da rota — a página inteira some. Sem o fix, este teste
 * falha de verdade (nenhum limite de Suspense na árvore).
 */
import { render, screen } from "@testing-library/react"
import { Tabs, TabsList, TabsTrigger, TabsPanel } from "@/components/ui/Tabs/Tabs"

function createSuspender() {
  let resolved = false
  let resolveFn: () => void = () => {}
  const promise = new Promise<void>((res) => {
    resolveFn = () => {
      resolved = true
      res()
    }
  })
  function LateNamespaceContent() {
    if (!resolved) throw promise
    return <p>Conteúdo do outro namespace</p>
  }
  return { LateNamespaceContent, resolve: resolveFn }
}

describe("TabsPanel — rede de segurança do Suspense (R3-8.1)", () => {
  it("aba com componente de outro namespace suspenso mostra o fallback só NO PAINEL, sem sumir com a página", async () => {
    const { LateNamespaceContent, resolve } = createSuspender()

    render(
      <div>
        <p>Página por trás das abas</p>
        <Tabs value="a" onValueChange={() => {}}>
          <TabsList ariaLabel="Seções">
            <TabsTrigger value="a">Config</TabsTrigger>
            <TabsTrigger value="b">Dashboard</TabsTrigger>
          </TabsList>
          <TabsPanel value="a">
            <LateNamespaceContent />
          </TabsPanel>
          <TabsPanel value="b">Painel B</TabsPanel>
        </Tabs>
      </div>,
    )

    // A página e a lista de abas continuam visíveis — só o painel ativo
    // mostra o fallback compacto.
    expect(screen.getByText("Página por trás das abas")).toBeInTheDocument()
    expect(screen.getByRole("tab", { name: "Config" })).toBeInTheDocument()
    expect(screen.getByRole("status")).toBeInTheDocument()
    expect(screen.queryByText("Conteúdo do outro namespace")).not.toBeInTheDocument()

    resolve()
    expect(await screen.findByText("Conteúdo do outro namespace")).toBeInTheDocument()
    expect(screen.getByText("Página por trás das abas")).toBeInTheDocument()
  })
})
