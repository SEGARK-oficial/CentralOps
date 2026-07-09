/**
 * Testes de Skeleton — render padrão, variantes, acessibilidade.
 */

import { render, screen } from "@testing-library/react"
import { Skeleton, SkeletonText, SkeletonCard, SkeletonTable } from "@/components/ui/Skeleton"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

// ── Skeleton base ─────────────────────────────────────────────────────────────

describe("Skeleton — base", () => {
  it("renderiza div com animate-pulse", () => {
    const { container } = render(<Skeleton />)
    const el = container.firstChild as HTMLElement
    expect(el.tagName).toBe("DIV")
    expect(el.className).toContain("animate-pulse")
  })

  it("é aria-hidden (invisível para leitores de tela)", () => {
    const { container } = render(<Skeleton />)
    const el = container.firstChild as HTMLElement
    expect(el).toHaveAttribute("aria-hidden", "true")
  })

  it("aceita className extra", () => {
    const { container } = render(<Skeleton className="h-8 w-32" />)
    const el = container.firstChild as HTMLElement
    expect(el.className).toContain("h-8")
    expect(el.className).toContain("w-32")
  })

  it("aplica width e height inline quando fornecidos", () => {
    const { container } = render(<Skeleton width="200px" height="1.5rem" />)
    const el = container.firstChild as HTMLElement
    expect(el.style.width).toBe("200px")
    expect(el.style.height).toBe("1.5rem")
  })
})

// ── SkeletonText ──────────────────────────────────────────────────────────────

describe("SkeletonText", () => {
  it("renderiza 3 linhas por padrão", () => {
    render(<SkeletonText />)
    // O container pai tem role=status; as linhas internas são aria-hidden
    const container = screen.getByRole("status")
    const lines = container.querySelectorAll("[aria-hidden='true']")
    expect(lines).toHaveLength(3)
  })

  it("renderiza N linhas quando especificado", () => {
    render(<SkeletonText lines={5} />)
    const container = screen.getByRole("status")
    const lines = container.querySelectorAll("[aria-hidden='true']")
    expect(lines).toHaveLength(5)
  })

  it("tem role=status e aria-label de carregamento", () => {
    render(<SkeletonText />)
    const el = screen.getByRole("status")
    expect(el).toHaveAttribute("aria-label", "Carregando texto…")
  })

  it("a última linha tem width 60% (simulação de parágrafo)", () => {
    render(<SkeletonText lines={3} />)
    const container = screen.getByRole("status")
    const lines = container.querySelectorAll<HTMLElement>("[aria-hidden='true']")
    expect(lines[2].style.width).toBe("60%")
  })
})

// ── SkeletonCard ──────────────────────────────────────────────────────────────

describe("SkeletonCard", () => {
  it("tem role=status e aria-label de carregamento", () => {
    render(<SkeletonCard />)
    const el = screen.getByRole("status", { name: /carregando card/i })
    expect(el).toBeInTheDocument()
  })

  it("renderiza a estrutura de avatar + linhas de corpo", () => {
    render(<SkeletonCard lines={4} />)
    const card = screen.getByRole("status", { name: /carregando card/i })
    // Tem pelo menos o avatar (círculo) + linhas de corpo
    const skeletons = card.querySelectorAll("[aria-hidden='true']")
    // 2 do header (título + subtítulo) + 1 avatar + 4 corpo = 7
    expect(skeletons.length).toBeGreaterThanOrEqual(5)
  })

  it("aceita className extra", () => {
    render(<SkeletonCard className="max-w-sm" />)
    const el = screen.getByRole("status", { name: /carregando card/i })
    expect(el.className).toContain("max-w-sm")
  })
})

// ── SkeletonTable ─────────────────────────────────────────────────────────────

describe("SkeletonTable", () => {
  it("tem role=status e aria-label de carregamento", () => {
    render(<SkeletonTable />)
    const el = screen.getByRole("status", { name: /carregando tabela/i })
    expect(el).toBeInTheDocument()
  })

  it("renderiza rows × columns células (+ cabeçalho)", () => {
    render(<SkeletonTable rows={3} columns={4} />)
    const table = screen.getByRole("status")
    // Cabeçalho: 4 células + 3 linhas × 4 = 16; total = 16 aria-hidden divs
    const cells = table.querySelectorAll("[aria-hidden='true']")
    expect(cells).toHaveLength(4 + 3 * 4)
  })

  it("usa defaults rows=5, columns=4 quando não especificado", () => {
    render(<SkeletonTable />)
    const table = screen.getByRole("status")
    const cells = table.querySelectorAll("[aria-hidden='true']")
    // 4 header + 5*4 = 24
    expect(cells).toHaveLength(4 + 5 * 4)
  })
})
