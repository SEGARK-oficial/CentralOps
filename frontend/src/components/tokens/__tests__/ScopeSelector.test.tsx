import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"

import { ScopeSelector } from "@/components/tokens/ScopeSelector"
import type { ScopeName } from "@/types"

vi.mock("@/services/api", () => ({
  listScopes: vi.fn(),
}))

import * as api from "@/services/api"

const SAMPLE_SCOPES: ScopeName[] = [
  "mapping.read",
  "mapping.write",
  "integration.read",
  "audit.read",
  "user.manage",
  "internal.tenant.read",
]

describe("ScopeSelector", () => {
  beforeEach(() => {
    vi.clearAllMocks()
    ;(api.listScopes as ReturnType<typeof vi.fn>).mockResolvedValue(SAMPLE_SCOPES)
  })

  it("loads scopes from API and groups by category", async () => {
    render(<ScopeSelector value={null} onChange={() => {}} />)

    // Wait for loading to finish
    expect(screen.getByText(/Carregando lista de scopes/i)).toBeInTheDocument()
    await waitFor(() => expect(api.listScopes).toHaveBeenCalled())

    // Two radio cards visible: full inherit + restrict
    expect(
      await screen.findByText(/Herdar permissões da conta/i),
    ).toBeInTheDocument()
    expect(screen.getByText(/Restringir a scopes específicos/i)).toBeInTheDocument()
  })

  it("default state: full inherit selected, no checkboxes shown", async () => {
    render(<ScopeSelector value={null} onChange={() => {}} />)
    await screen.findByText(/Herdar permissões da conta/i)

    // The full-inherit radio should be checked.
    const inherit = screen.getByLabelText(/Herdar permissões da conta/i, {
      exact: false,
    }) as HTMLInputElement
    expect(inherit.checked).toBe(true)

    // No scope checkbox should be visible while inherit is selected.
    expect(screen.queryByText("mapping.read")).not.toBeInTheDocument()
  })

  it("switching to restrict mode reveals scope checkboxes", async () => {
    // Regressão: `value === [] || value === null` colapsavam no mesmo estado
    // visual ("herdar" continuava marcado, grid nunca abria) porque o radio
    // de restringir era calculado só a partir de `value`, e `onChange([])`
    // produz um `value` que passa exatamente por esse buraco. O teste
    // ANTERIOR mascarava isto montando uma instância NOVA com
    // `requireExplicit` em vez de deixar a MESMA instância re-renderizar —
    // por isso re-render aqui, não `render()` de novo.
    let currentValue: ScopeName[] | null = null
    const onChange = vi.fn((next: ScopeName[] | null) => {
      currentValue = next
    })

    const { rerender } = render(
      <ScopeSelector value={currentValue} onChange={onChange} />,
    )
    await screen.findByText(/Restringir a scopes específicos/i)

    fireEvent.click(screen.getByText(/Restringir a scopes específicos/i))
    expect(onChange).toHaveBeenCalledWith([])

    rerender(<ScopeSelector value={currentValue} onChange={onChange} />)

    const restrict = screen.getByLabelText(/Restringir a scopes específicos/i, {
      exact: false,
    }) as HTMLInputElement
    expect(restrict.checked).toBe(true)
    await waitFor(() =>
      expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(0),
    )
  })

  it("toggling a scope adds it to the selection", async () => {
    const onChange = vi.fn()
    render(
      <ScopeSelector value={[]} onChange={onChange} requireExplicit />,
    )
    await waitFor(() =>
      expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(0),
    )

    const checkboxes = screen.getAllByRole("checkbox")
    const mappingReadCb = checkboxes.find((c) =>
      c.parentElement?.textContent?.includes("mapping.read"),
    )
    expect(mappingReadCb).toBeTruthy()
    fireEvent.click(mappingReadCb!)
    expect(onChange).toHaveBeenCalledWith(["mapping.read"])
  })

  it("toggling the only selected scope back to empty stays em modo restrito (não vira herdar)", async () => {
    // Antes: esvaziar a seleção chamava `onChange(null)`, que troca o radio de
    // volta para "herdar" SOZINHO — sem nenhum clique do usuário no radio de
    // herdar, e herdar é a role INTEIRA, o oposto de restringir. Ficar em `[]`
    // preserva a escolha explícita; o aviso de "zero = herda tudo" é quem
    // avisa o usuário, não uma troca de radio pelas costas dele.
    const onChange = vi.fn()
    render(<ScopeSelector value={["mapping.read"]} onChange={onChange} />)
    await waitFor(() =>
      expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(2),
    )

    const checkboxes = screen.getAllByRole("checkbox")
    const mappingReadCb = checkboxes.find((c) =>
      c.parentElement?.textContent?.includes("mapping.read"),
    )
    fireEvent.click(mappingReadCb!)
    expect(onChange).toHaveBeenCalledWith([])
  })

  it("mostra aviso quando o modo restrito fica com zero scopes marcados", async () => {
    // O backend trata lista vazia igual a ausente (`not token_scopes` é
    // verdadeiro pra `[]` e pra `None` em Python) — token com zero marcado
    // sai IDÊNTICO a herdar tudo, não a "sem acesso nenhum". Sem aviso isso
    // parece least privilege e na prática libera a role inteira.
    render(<ScopeSelector value={[]} onChange={() => {}} requireExplicit />)
    expect(
      await screen.findByText(/equivale a herdar tudo/i),
    ).toBeInTheDocument()
  })

  it("requireExplicit=true hides the inherit option", async () => {
    render(
      <ScopeSelector value={[]} onChange={() => {}} requireExplicit />,
    )
    await screen.findByText(/Restringir a scopes específicos/i)
    // `queryByRole` em vez de `queryByText`: o aviso de "zero scopes" também
    // menciona herdar em prosa quando `!requireExplicit`, então checar por
    // texto solto pegaria essa menção em vez do radio que deve sumir.
    expect(
      screen.queryByRole("radio", { name: /Herdar permissões da conta/i }),
    ).not.toBeInTheDocument()
  })

  it("renders error notice when scope listing fails", async () => {
    ;(api.listScopes as ReturnType<typeof vi.fn>).mockRejectedValueOnce(
      new Error("network down"),
    )
    render(<ScopeSelector value={null} onChange={() => {}} />)
    expect(await screen.findByText(/Falha ao listar scopes/i)).toBeInTheDocument()
  })

  it("disabled prop blocks interactions", async () => {
    const onChange = vi.fn()
    render(
      <ScopeSelector value={[]} onChange={onChange} requireExplicit disabled />,
    )
    await waitFor(() =>
      expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(0),
    )

    const cb = screen.getAllByRole("checkbox").find((c) =>
      c.parentElement?.textContent?.includes("mapping.read"),
    )!
    expect(cb).toBeDisabled()
    fireEvent.click(cb)
    expect(onChange).not.toHaveBeenCalled()
  })

  it("clicar em restringir marca o radio e permite marcar um scope, sem requireExplicit", async () => {
    // Reproduz o fluxo real do modal de criação de token (TokensPage.tsx não
    // passa `requireExplicit`): estado inicial null (herdar), clique em
    // "Restringir", clique num scope — tudo na MESMA instância do componente,
    // como o React realmente re-renderiza em produção.
    let currentValue: ScopeName[] | null = null
    const onChange = vi.fn((next: ScopeName[] | null) => {
      currentValue = next
    })
    const { rerender } = render(
      <ScopeSelector value={currentValue} onChange={onChange} />,
    )
    await screen.findByText(/Restringir a scopes específicos/i)

    fireEvent.click(screen.getByText(/Restringir a scopes específicos/i))
    rerender(<ScopeSelector value={currentValue} onChange={onChange} />)

    const checkboxes = screen.getAllByRole("checkbox")
    const mappingReadCb = checkboxes.find((c) =>
      c.parentElement?.textContent?.includes("mapping.read"),
    )
    expect(mappingReadCb).toBeTruthy()
    fireEvent.click(mappingReadCb!)
    expect(onChange).toHaveBeenLastCalledWith(["mapping.read"])
  })

  it("displays a counter when at least one scope is selected", async () => {
    render(
      <ScopeSelector
        value={["mapping.read", "integration.read"]}
        onChange={() => {}}
      />,
    )
    // Wait for scope grid to render (the ``2`` count appears at the bottom).
    await waitFor(() =>
      expect(screen.getAllByRole("checkbox").length).toBeGreaterThan(2),
    )
    // The counter renders ``<strong>2</strong> scopes selecionado(s)`` —
    // get the exact strong element to disambiguate from descriptions that
    // contain the word "scopes".
    const counter = screen.getByText("2", { selector: "strong" })
    expect(counter).toBeInTheDocument()
  })
})
