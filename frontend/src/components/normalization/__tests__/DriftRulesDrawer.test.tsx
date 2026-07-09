/**
 * Testes de DriftRulesDrawer — Fase 4.3
 * Cobre: render da lista, badges de match_kind, link "Abrir mapping",
 *        estado vazio, fechar com botão ×, fechar com Escape.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { DriftRulesDrawer } from "@/components/normalization/DriftRulesDrawer"
import type { MatchedRule } from "@/hooks/useFieldRules"

// ── Fixtures ──────────────────────────────────────────────────────────────────

const RULE_PRIMARY: MatchedRule = {
  rule_target: "normalized.severity",
  source: "threat.severity",
  match_kind: "primary",
  mapping_definition_id: "def-sophos-001",
  vendor: "sophos",
  event_type: "detection",
}

const RULE_FALLBACK: MatchedRule = {
  rule_target: "normalized.user",
  source: "user.name",
  match_kind: "fallback",
  mapping_definition_id: "def-sophos-001",
  vendor: "sophos",
  event_type: "detection",
}

const RULE_ARRAY: MatchedRule = {
  rule_target: "normalized.observables",
  source: "network.sourceIp",
  match_kind: "array_builder_item",
  mapping_definition_id: "def-sophos-002",
  vendor: "sophos",
  event_type: "detection",
}

const RULE_PREPROCESS: MatchedRule = {
  rule_target: "_parsed",
  source: "details.rawData",
  match_kind: "preprocess",
  mapping_definition_id: "def-sophos-001",
  vendor: "sophos",
  event_type: "detection",
}

// ── Helper ────────────────────────────────────────────────────────────────────

function renderDrawer(
  rules: MatchedRule[] = [RULE_PRIMARY],
  open = true,
  onClose = vi.fn(),
  field_path = "threat.severity",
) {
  return render(
    <MemoryRouter>
      <DriftRulesDrawer
        open={open}
        onClose={onClose}
        field_path={field_path}
        rules={rules}
      />
    </MemoryRouter>,
  )
}

// ── Render ────────────────────────────────────────────────────────────────────

describe("DriftRulesDrawer — render", () => {
  it("renderiza o título com field_path no heading", () => {
    renderDrawer([RULE_PRIMARY], true, vi.fn(), "threat.severity")
    const heading = screen.getByRole("heading", { name: "threat.severity" })
    expect(heading).toBeInTheDocument()
  })

  it("não renderiza nada quando open=false", () => {
    renderDrawer([RULE_PRIMARY], false)
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument()
  })

  it("tem role=dialog e aria-modal=true", () => {
    renderDrawer()
    const dialog = screen.getByRole("dialog")
    expect(dialog).toHaveAttribute("aria-modal", "true")
  })

  it("tem aria-labelledby apontando para o título", () => {
    renderDrawer()
    const dialog = screen.getByRole("dialog")
    const labelId = dialog.getAttribute("aria-labelledby")
    expect(labelId).not.toBeNull()
    const title = document.getElementById(labelId!)
    expect(title).toBeInTheDocument()
    expect(title!.textContent?.trim()).toBe("threat.severity")
  })
})

// ── Lista de regras ───────────────────────────────────────────────────────────

describe("DriftRulesDrawer — lista de regras", () => {
  it("renderiza rule target de regra primária", () => {
    renderDrawer([RULE_PRIMARY])
    expect(screen.getByText("normalized.severity")).toBeInTheDocument()
  })

  it("renderiza source da regra no corpo do item", () => {
    renderDrawer([RULE_PRIMARY])
    // O source aparece dentro de um <span class="font-mono">
    const sourceEl = screen.getAllByText("threat.severity").find(
      (el) => el.tagName === "SPAN" && el.className.includes("font-mono"),
    )
    expect(sourceEl).toBeDefined()
  })

  it("badge Primário para match_kind=primary", () => {
    renderDrawer([RULE_PRIMARY])
    expect(screen.getByTestId("drawer-rule-kind-0")).toHaveTextContent("Primário")
  })

  it("badge Fallback para match_kind=fallback", () => {
    renderDrawer([RULE_FALLBACK], true, vi.fn(), "user.name")
    expect(screen.getByTestId("drawer-rule-kind-0")).toHaveTextContent("Fallback")
  })

  it("badge Array Item para match_kind=array_builder_item", () => {
    renderDrawer([RULE_ARRAY], true, vi.fn(), "network.sourceIp")
    expect(screen.getByTestId("drawer-rule-kind-0")).toHaveTextContent("Array Item")
  })

  it("badge Preprocess para match_kind=preprocess", () => {
    renderDrawer([RULE_PREPROCESS], true, vi.fn(), "details.rawData")
    expect(screen.getByTestId("drawer-rule-kind-0")).toHaveTextContent("Preprocess")
  })

  it("renderiza múltiplas regras na lista", () => {
    renderDrawer([RULE_PRIMARY, RULE_FALLBACK, RULE_ARRAY], true, vi.fn(), "threat.severity")
    expect(screen.getByTestId("drawer-rule-item-0")).toBeInTheDocument()
    expect(screen.getByTestId("drawer-rule-item-1")).toBeInTheDocument()
    expect(screen.getByTestId("drawer-rule-item-2")).toBeInTheDocument()
  })
})

// ── Link "Abrir mapping" ──────────────────────────────────────────────────────

describe("DriftRulesDrawer — link Abrir mapping", () => {
  it("link aponta para /mappings/{mapping_definition_id}", () => {
    renderDrawer([RULE_PRIMARY])
    const link = screen.getByTestId("drawer-rule-link-0")
    expect(link).toHaveAttribute("href", "/mappings/def-sophos-001")
  })

  it("cada regra tem seu próprio link com o ID correto", () => {
    renderDrawer([RULE_PRIMARY, RULE_ARRAY])
    expect(screen.getByTestId("drawer-rule-link-0")).toHaveAttribute(
      "href",
      "/mappings/def-sophos-001",
    )
    expect(screen.getByTestId("drawer-rule-link-1")).toHaveAttribute(
      "href",
      "/mappings/def-sophos-002",
    )
  })
})

// ── Estado vazio ──────────────────────────────────────────────────────────────

describe("DriftRulesDrawer — estado vazio", () => {
  it("mostra 'Nenhuma regra encontrada.' quando rules=[]", () => {
    renderDrawer([])
    expect(screen.getByText("Nenhuma regra encontrada.")).toBeInTheDocument()
  })

  it("não renderiza lista quando rules vazio", () => {
    renderDrawer([])
    expect(screen.queryByTestId("drawer-rules-list")).not.toBeInTheDocument()
  })
})

// ── Fechar ────────────────────────────────────────────────────────────────────

describe("DriftRulesDrawer — fechar", () => {
  it("botão × chama onClose", () => {
    const onClose = vi.fn()
    renderDrawer([RULE_PRIMARY], true, onClose)
    fireEvent.click(screen.getByRole("button", { name: /fechar drawer/i }))
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("tecla Escape chama onClose", () => {
    const onClose = vi.fn()
    renderDrawer([RULE_PRIMARY], true, onClose)
    fireEvent.keyDown(document, { key: "Escape", code: "Escape" })
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("clique no overlay (fora do drawer) chama onClose", () => {
    const onClose = vi.fn()
    renderDrawer([RULE_PRIMARY], true, onClose)
    // O overlay é o elemento com bg-black/40 — o primeiro filho do portal
    const overlay = document.querySelector(".bg-black\\/40") as HTMLElement
    expect(overlay).not.toBeNull()
    fireEvent.click(overlay)
    expect(onClose).toHaveBeenCalledTimes(1)
  })

  it("clique dentro do drawer NÃO chama onClose", () => {
    const onClose = vi.fn()
    renderDrawer([RULE_PRIMARY], true, onClose)
    fireEvent.click(screen.getByRole("dialog"))
    expect(onClose).not.toHaveBeenCalled()
  })
})
