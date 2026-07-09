/**
 * Testes de EnvelopePreview
 * Cobre: loading, empty, resultado com output_examples, falhas, erro de API.
 */

import { render, screen } from "@testing-library/react"
import { EnvelopePreview } from "@/components/mappings/EnvelopePreview"
import type { DryRunResult } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const RESULT_OK: DryRunResult = {
  sample_size: 10,
  ok_count: 10,
  fail_count: 0,
  rule_failures: [],
  output_examples: [{ event: { action: "login" } }],
  default_hit_warnings: [],
}

const RESULT_WITH_FAILURES: DryRunResult = {
  sample_size: 10,
  ok_count: 7,
  fail_count: 3,
  rule_failures: [
    {
      target: "event.action",
      fail_count: 3,
      fail_examples: ["valor_inesperado"],
    },
  ],
  output_examples: [{ event: { action: "partial" } }],
  default_hit_warnings: [],
}

describe("EnvelopePreview", () => {
  it("painel tem role=region com aria-labelledby", () => {
    render(
      <EnvelopePreview result={null} isPending={false} error={null} />,
    )
    const region = screen.getByTestId("envelope-preview")
    expect(region).toHaveAttribute("role", "region")
    expect(region).toHaveAttribute("aria-labelledby")
  })

  it("estado vazio (sem resultado, sem erro, sem pending) mostra EmptyState", () => {
    render(<EnvelopePreview result={null} isPending={false} error={null} />)
    expect(
      screen.getByText("Forneça uma amostra para ver a normalização."),
    ).toBeInTheDocument()
  })

  it("estado loading (isPending=true, sem resultado) mostra spinner", () => {
    render(<EnvelopePreview result={null} isPending={true} error={null} />)
    expect(screen.getByText("Calculando normalização...")).toBeInTheDocument()
  })

  it("estado loading com resultado anterior exibe DryRunStatusBar com isPending", () => {
    render(
      <EnvelopePreview result={RESULT_OK} isPending={true} error={null} />,
    )
    expect(screen.getByTestId("dry-run-status-bar")).toBeInTheDocument()
    // EmptyState NÃO deve aparecer quando há resultado
    expect(
      screen.queryByText("Forneça uma amostra"),
    ).not.toBeInTheDocument()
  })

  it("resultado OK: DryRunStatusBar com contagens corretas", () => {
    render(
      <EnvelopePreview result={RESULT_OK} isPending={false} error={null} />,
    )
    expect(screen.getByText("10 amostras")).toBeInTheDocument()
    expect(screen.getByText("10 OK")).toBeInTheDocument()
    // CLDR pluraliza "pt" (não "pt-BR") com categoria "one" para n=0 (i18next _one).
    expect(screen.getByText("0 falha")).toBeInTheDocument()
  })

  it("resultado com falhas: Notice de aviso visível", () => {
    render(
      <EnvelopePreview
        result={RESULT_WITH_FAILURES}
        isPending={false}
        error={null}
      />,
    )
    expect(screen.getByText("Falhas de regras detectadas")).toBeInTheDocument()
    expect(screen.getByText(/event\.action/)).toBeInTheDocument()
    expect(screen.getByText(/valor_inesperado/)).toBeInTheDocument()
  })

  it("erro de API: Notice de erro visível", () => {
    const err = new Error("Timeout na chamada ao backend")
    render(
      <EnvelopePreview result={null} isPending={false} error={err} />,
    )
    expect(screen.getByText("Erro na simulação")).toBeInTheDocument()
    expect(screen.getByText("Timeout na chamada ao backend")).toBeInTheDocument()
  })

  it("erro de API não exibe EmptyState", () => {
    const err = new Error("Erro qualquer")
    render(
      <EnvelopePreview result={null} isPending={false} error={err} />,
    )
    expect(
      screen.queryByText("Forneça uma amostra"),
    ).not.toBeInTheDocument()
  })

  it("DryRunStatusBar data-testid presente no resultado", () => {
    render(
      <EnvelopePreview result={RESULT_OK} isPending={false} error={null} />,
    )
    expect(screen.getByTestId("dry-run-status-bar")).toBeInTheDocument()
  })
})
