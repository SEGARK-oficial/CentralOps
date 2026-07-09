/**
 * Testes de MappingDiffModal
 * Cobre: render de added/removed/modified, reordered_only, sem diff, loading.
 */

import { render, screen, fireEvent } from "@testing-library/react"
import { MappingDiffModal } from "@/components/mappings/MappingDiffModal"
import type { MappingVersionDiff } from "@/lib/mappingDiff"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const DIFF_WITH_ALL: MappingVersionDiff = {
  reordered_only: false,
  added: [{ target: "new.field", source: "nf" }],
  removed: [{ target: "old.field", source: "of" }],
  modified: [
    {
      target: "changed.field",
      before: { target: "changed.field", source: "old_source" },
      after: { target: "changed.field", source: "new_source" },
    },
  ],
}

const DIFF_REORDERED: MappingVersionDiff = {
  reordered_only: true,
  added: [],
  removed: [],
  modified: [],
}

const DIFF_EMPTY: MappingVersionDiff = {
  reordered_only: false,
  added: [],
  removed: [],
  modified: [],
}

describe("MappingDiffModal", () => {
  it("renderiza quando open=true com diff completo", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={DIFF_WITH_ALL} />)
    expect(screen.getByTestId("diff-modal")).toBeInTheDocument()
  })

  it("não renderiza quando open=false", () => {
    render(<MappingDiffModal open={false} onClose={vi.fn()} diff={DIFF_WITH_ALL} />)
    expect(screen.queryByTestId("diff-modal")).not.toBeInTheDocument()
  })

  it("exibe seção 'Adicionadas' com badge de contagem", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={DIFF_WITH_ALL} />)
    expect(screen.getByText("Adicionadas")).toBeInTheDocument()
  })

  it("exibe seção 'Removidas' com badge de contagem", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={DIFF_WITH_ALL} />)
    expect(screen.getByText("Removidas")).toBeInTheDocument()
  })

  it("exibe seção 'Modificadas' com badge de contagem", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={DIFF_WITH_ALL} />)
    expect(screen.getByText("Modificadas")).toBeInTheDocument()
  })

  it("exibe Notice 'Apenas reordenação' quando reordered_only=true", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={DIFF_REORDERED} />)
    expect(screen.getByText("Apenas reordenação")).toBeInTheDocument()
  })

  it("exibe Notice 'Sem alterações' quando diff vazio", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={DIFF_EMPTY} />)
    expect(screen.getByText("Sem alterações")).toBeInTheDocument()
  })

  it("exibe notice 'Nenhum diff disponível' quando diff=null", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={null} />)
    expect(screen.getByText("Nenhum diff disponível.")).toBeInTheDocument()
  })

  it("exibe 'Carregando diff...' quando isLoading=true", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={null} isLoading={true} />)
    expect(screen.getByText("Carregando diff...")).toBeInTheDocument()
  })

  it("exibe versionLabel no título quando fornecido", () => {
    render(<MappingDiffModal open={true} onClose={vi.fn()} diff={DIFF_WITH_ALL} versionLabel="v1 → v2" />)
    expect(screen.getByText("Diff de versões — v1 → v2")).toBeInTheDocument()
  })

  it("chama onClose quando ESC é pressionado", () => {
    const onClose = vi.fn()
    render(<MappingDiffModal open={true} onClose={onClose} diff={DIFF_EMPTY} />)
    fireEvent.keyDown(document, { key: "Escape" })
    expect(onClose).toHaveBeenCalled()
  })
})
