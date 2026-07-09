/**
 * PiiRuleEditor tests.
 *
 * Cobre:
 * - Render padrão (modo estruturado, sem regras).
 * - Adicionar regra: onChange com nova PiiRedactionRule.
 * - Remover regra: onChange sem o item.
 * - Mudar action: campos condicionais exibidos (mask → mask_char/fixed_len).
 * - Ação drop_field não exibe campos extras.
 * - Preview de regra com path preenchido.
 * - Modo JSON: textarea renderizada ao clicar em "JSON avançado".
 * - Parse de JSON válido chama onChange.
 * - JSON inválido exibe aviso de erro.
 * - Aceita formato spec {version, rules} no JSON.
 * - A11y: botão de remover tem aria-label descritivo.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import { PiiRuleEditor } from "../PiiRuleEditor"
import i18n from "@/i18n"
import type { PiiRedactionRule } from "@/types"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

const RULE_MASK: PiiRedactionRule = {
  path: "raw.user.email",
  action: "mask",
}

const RULE_HASH: PiiRedactionRule = {
  path: "raw.user.id",
  action: "hash",
  salt: "s3cr3t",
}

describe("PiiRuleEditor — render padrão", () => {
  it("renderiza sem regras com texto explicativo", () => {
    const onChange = vi.fn()
    render(<PiiRuleEditor rules={[]} onChange={onChange} />)
    expect(screen.getByText(/Nenhuma regra de PII configurada/i)).toBeInTheDocument()
    expect(screen.getByText("Adicionar regra PII")).toBeInTheDocument()
  })

  it("exibe contagem de regras quando há regras", () => {
    render(<PiiRuleEditor rules={[RULE_MASK]} onChange={vi.fn()} />)
    expect(screen.getByText(/1 regra configurada/i)).toBeInTheDocument()
  })
})

describe("PiiRuleEditor — adicionar e remover regras", () => {
  it("clicar em 'Adicionar regra PII' chama onChange com regra padrão appended", () => {
    const onChange = vi.fn()
    render(<PiiRuleEditor rules={[]} onChange={onChange} />)
    fireEvent.click(screen.getByText("Adicionar regra PII"))
    expect(onChange).toHaveBeenCalledOnce()
    const [called] = onChange.mock.calls[0] as [PiiRedactionRule[]]
    expect(called).toHaveLength(1)
    expect(called[0].action).toBe("mask")
  })

  it("clicar em remover chama onChange sem o item", () => {
    const onChange = vi.fn()
    render(<PiiRuleEditor rules={[RULE_MASK, RULE_HASH]} onChange={onChange} />)
    const removeBtn = screen.getAllByRole("button", { name: /Remover regra PII/i })[0]
    fireEvent.click(removeBtn)
    const [called] = onChange.mock.calls[0] as [PiiRedactionRule[]]
    expect(called).toHaveLength(1)
    expect(called[0].path).toBe("raw.user.id")
  })

  it("botão de remover tem aria-label descritivo com número da regra", () => {
    render(<PiiRuleEditor rules={[RULE_MASK]} onChange={vi.fn()} />)
    const btn = screen.getByRole("button", { name: /Remover regra PII 1/i })
    expect(btn).toBeInTheDocument()
  })
})

describe("PiiRuleEditor — campos condicionais por ação", () => {
  it("ação mask exibe campos condicionais acessíveis para mask_char e fixed_len", () => {
    render(<PiiRuleEditor rules={[RULE_MASK]} onChange={vi.fn()} />)
    // Os inputs têm aria-label descritivo
    expect(screen.getByRole("textbox", { name: /Caractere de máscara/i })).toBeInTheDocument()
    expect(screen.getByRole("spinbutton", { name: /Comprimento fixo da máscara/i })).toBeInTheDocument()
  })

  it("ação hash exibe campo salt acessível", () => {
    render(<PiiRuleEditor rules={[RULE_HASH]} onChange={vi.fn()} />)
    expect(screen.getByRole("textbox", { name: /Salt para hash/i })).toBeInTheDocument()
  })

  it("ação partial exibe keep_prefix, keep_suffix e octets acessíveis", () => {
    render(<PiiRuleEditor rules={[{ path: "src_ip", action: "partial" }]} onChange={vi.fn()} />)
    expect(screen.getByRole("spinbutton", { name: /Número de caracteres a manter no prefixo/i })).toBeInTheDocument()
    expect(screen.getByRole("spinbutton", { name: /Número de caracteres a manter no sufixo/i })).toBeInTheDocument()
    expect(screen.getByRole("spinbutton", { name: /Número de octetos IP/i })).toBeInTheDocument()
  })

  it("ação drop_field não exibe campos extras", () => {
    render(<PiiRuleEditor rules={[{ path: "raw.ssn", action: "drop_field" }]} onChange={vi.fn()} />)
    expect(screen.queryByRole("textbox", { name: /Salt para hash/i })).not.toBeInTheDocument()
    expect(screen.queryByRole("spinbutton", { name: /Comprimento fixo/i })).not.toBeInTheDocument()
  })
})

describe("PiiRuleEditor — preview de regra", () => {
  it("exibe preview quando path está preenchido", () => {
    render(<PiiRuleEditor rules={[RULE_MASK]} onChange={vi.fn()} />)
    expect(screen.getByText(/raw\.user\.email → mask/i)).toBeInTheDocument()
  })
})

describe("PiiRuleEditor — modo JSON avançado", () => {
  it("clicar em 'JSON avançado' exibe textarea", () => {
    render(<PiiRuleEditor rules={[]} onChange={vi.fn()} />)
    fireEvent.click(screen.getByText("JSON avançado"))
    expect(screen.getByRole("textbox", { name: /Regras PII em formato JSON/i })).toBeInTheDocument()
  })

  it("JSON válido no blur chama onChange com regras parseadas", async () => {
    const onChange = vi.fn()
    render(<PiiRuleEditor rules={[]} onChange={onChange} />)
    fireEvent.click(screen.getByText("JSON avançado"))
    const textarea = screen.getByRole("textbox", { name: /Regras PII em formato JSON/i })
    fireEvent.change(textarea, { target: { value: '[{"path":"raw.email","action":"mask"}]' } })
    fireEvent.blur(textarea)
    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith([{ path: "raw.email", action: "mask" }])
    })
  })

  it("JSON inválido no blur exibe aviso de erro", () => {
    render(<PiiRuleEditor rules={[]} onChange={vi.fn()} />)
    fireEvent.click(screen.getByText("JSON avançado"))
    const textarea = screen.getByRole("textbox", { name: /Regras PII em formato JSON/i })
    fireEvent.change(textarea, { target: { value: "{invalid}" } })
    fireEvent.blur(textarea)
    // Verifica pela presença do role=alert do Notice
    const alerts = screen.getAllByRole("alert")
    const hasJsonError = alerts.some((el) => el.textContent?.includes("JSON inválido"))
    expect(hasJsonError).toBe(true)
  })

  it("aceita formato spec {version, rules} no JSON", async () => {
    const onChange = vi.fn()
    render(<PiiRuleEditor rules={[]} onChange={onChange} />)
    fireEvent.click(screen.getByText("JSON avançado"))
    const textarea = screen.getByRole("textbox", { name: /Regras PII em formato JSON/i })
    const spec = JSON.stringify({ version: 1, rules: [{ path: "raw.ip", action: "partial" }] })
    fireEvent.change(textarea, { target: { value: spec } })
    fireEvent.blur(textarea)
    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith([{ path: "raw.ip", action: "partial" }])
    })
  })

  it("alternar de JSON para estruturado com JSON válido sincroniza regras", async () => {
    const onChange = vi.fn()
    render(<PiiRuleEditor rules={[]} onChange={onChange} />)
    fireEvent.click(screen.getByText("JSON avançado"))
    const textarea = screen.getByRole("textbox", { name: /Regras PII em formato JSON/i })
    fireEvent.change(textarea, { target: { value: '[{"path":"raw.email","action":"hash"}]' } })
    fireEvent.click(screen.getByText("Modo estruturado"))
    await waitFor(() => {
      expect(onChange).toHaveBeenCalledWith([{ path: "raw.email", action: "hash" }])
    })
  })

  it("alternar para estruturado com JSON inválido mantém modo JSON com erro", () => {
    render(<PiiRuleEditor rules={[]} onChange={vi.fn()} />)
    fireEvent.click(screen.getByText("JSON avançado"))
    const textarea = screen.getByRole("textbox", { name: /Regras PII em formato JSON/i })
    fireEvent.change(textarea, { target: { value: "!!invalid" } })
    fireEvent.click(screen.getByText("Modo estruturado"))
    // Deve permanecer em modo JSON e exibir erro
    const alerts = screen.getAllByRole("alert")
    const hasError = alerts.some((el) => el.textContent?.includes("JSON inválido"))
    expect(hasError).toBe(true)
    // Textarea ainda está visível
    expect(screen.getByRole("textbox", { name: /Regras PII em formato JSON/i })).toBeInTheDocument()
  })
})
