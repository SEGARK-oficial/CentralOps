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
    const onChange = vi.fn()
    render(<ScopeSelector value={null} onChange={onChange} />)
    await screen.findByText(/Restringir a scopes específicos/i)

    fireEvent.click(screen.getByText(/Restringir a scopes específicos/i))
    expect(onChange).toHaveBeenCalledWith([])

    // ``requireExplicit`` opens the grid without the inherit toggle —
    // simulates the "restrict mode" state without re-render gymnastics.
    render(<ScopeSelector value={[]} onChange={onChange} requireExplicit />)
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

  it("toggling the only selected scope back to empty calls onChange(null)", async () => {
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
    // Empty list → null pra sinalizar full inherit (legacy Fase 1).
    expect(onChange).toHaveBeenCalledWith(null)
  })

  it("requireExplicit=true hides the inherit option", async () => {
    render(
      <ScopeSelector value={[]} onChange={() => {}} requireExplicit />,
    )
    await screen.findByText(/Restringir a scopes específicos/i)
    expect(
      screen.queryByText(/Herdar permissões da conta/i),
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
