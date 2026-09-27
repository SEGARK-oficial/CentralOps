/**
 * useForm — cobre validação, submit e o `submitError` (antes o erro do
 * `onSubmit` rejeitado ia só pro `console.error` e o usuário ficava olhando
 * pro formulário sem nenhum sinal do que aconteceu).
 */
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react"
import { useForm } from "@/hooks/useForm"

interface FormValues {
  name: string
}

interface TwoFieldValues {
  title: string
  statement: string
}

// R2-8.3: harness com DOM real — `renderHook` não dá elemento nenhum para
// focar. Espelha o padrão real (CreateQueryForm/EditQueryModal): title
// validado antes de statement, e é isso que decide qual campo recebe o foco.
function TwoFieldForm({ onSubmit }: { onSubmit: (v: TwoFieldValues) => Promise<void> }) {
  const { values, errors, touched, handleChange, handleBlur, handleSubmit, registerField } = useForm<TwoFieldValues>({
    initialValues: { title: "", statement: "" },
    validate: (v) => {
      const errs: Partial<Record<keyof TwoFieldValues, string>> = {}
      if (!v.title.trim()) errs.title = "título obrigatório"
      if (!v.statement.trim()) errs.statement = "statement obrigatório"
      return errs
    },
    onSubmit,
  })

  return (
    <form onSubmit={handleSubmit}>
      <label htmlFor="title">Título</label>
      <input
        ref={registerField("title")}
        id="title"
        name="title"
        value={values.title}
        onChange={handleChange}
        onBlur={handleBlur}
        aria-invalid={touched.title && errors.title ? "true" : undefined}
      />
      <label htmlFor="statement">Statement</label>
      <textarea
        ref={registerField("statement")}
        id="statement"
        name="statement"
        value={values.statement}
        onChange={handleChange}
        onBlur={handleBlur}
        aria-invalid={touched.statement && errors.statement ? "true" : undefined}
      />
      <button type="submit">Salvar</button>
    </form>
  )
}

describe("useForm", () => {
  it("valida e bloqueia submit com erro de campo", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { result } = renderHook(() =>
      useForm<FormValues>({
        initialValues: { name: "" },
        validate: (v) => (v.name.trim() ? {} : { name: "obrigatório" }),
        onSubmit,
      }),
    )

    await act(async () => {
      result.current.handleSubmit({ preventDefault: () => {} } as React.FormEvent)
    })

    expect(result.current.errors.name).toBe("obrigatório")
    expect(onSubmit).not.toHaveBeenCalled()
  })

  it("chama onSubmit quando válido", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    const { result } = renderHook(() =>
      useForm<FormValues>({ initialValues: { name: "wazuh" }, onSubmit }),
    )

    await act(async () => {
      result.current.handleSubmit({ preventDefault: () => {} } as React.FormEvent)
    })

    expect(onSubmit).toHaveBeenCalledWith({ name: "wazuh" })
  })

  // Regressão: onSubmit rejeitado agora expõe `submitError` — antes o
  // catch só fazia `console.error` e não havia NENHUM jeito de o consumidor
  // saber que o submit falhou.
  it("expõe submitError quando onSubmit rejeita, e limpa no próximo submit", async () => {
    const onSubmit = vi.fn()
      .mockRejectedValueOnce(new Error("falha no backend"))
      .mockResolvedValueOnce(undefined)
    const { result } = renderHook(() =>
      useForm<FormValues>({ initialValues: { name: "wazuh" }, onSubmit }),
    )

    expect(result.current.submitError).toBeNull()

    await act(async () => {
      result.current.handleSubmit({ preventDefault: () => {} } as React.FormEvent)
    })
    await waitFor(() => expect(result.current.submitError).toBe("falha no backend"))

    await act(async () => {
      result.current.handleSubmit({ preventDefault: () => {} } as React.FormEvent)
    })
    await waitFor(() => expect(result.current.submitError).toBeNull())
  })

  // R4-8.5: o `submitError` já entrega o erro pro form — um `console.error`
  // paralelo só duplicava em produção. Sem o fix, este teste falha (o spy é
  // chamado).
  it("não faz console.error quando onSubmit rejeita — o erro já vira submitError", async () => {
    const consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => {})
    const onSubmit = vi.fn().mockRejectedValue(new Error("falha no backend"))
    const { result } = renderHook(() =>
      useForm<FormValues>({ initialValues: { name: "wazuh" }, onSubmit }),
    )

    await act(async () => {
      result.current.handleSubmit({ preventDefault: () => {} } as React.FormEvent)
    })
    await waitFor(() => expect(result.current.submitError).toBe("falha no backend"))

    expect(consoleErrorSpy).not.toHaveBeenCalled()
    consoleErrorSpy.mockRestore()
  })

  it("resetForm limpa submitError", async () => {
    const onSubmit = vi.fn().mockRejectedValue(new Error("x"))
    const { result } = renderHook(() =>
      useForm<FormValues>({ initialValues: { name: "wazuh" }, onSubmit }),
    )

    await act(async () => {
      result.current.handleSubmit({ preventDefault: () => {} } as React.FormEvent)
    })
    await waitFor(() => expect(result.current.submitError).toBe("x"))

    act(() => result.current.resetForm())
    expect(result.current.submitError).toBeNull()
  })

  // R2-8.3: `errors`/`touched` já existiam e já alimentavam `error=` no
  // Input/Textarea (aria-invalid/aria-describedby de graça) — o que faltava
  // era mover o FOCO. Sem isto, quem usa teclado/leitor de tela via só o
  // banner genérico e tinha que caçar manualmente qual campo, lá em cima,
  // ficou vermelho.
  describe("registerField — foco no 1º campo inválido (R2-8.3)", () => {
    it("submit com os dois campos vazios foca o PRIMEIRO validado (title)", async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(<TwoFieldForm onSubmit={onSubmit} />)

      fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

      await waitFor(() => expect(screen.getByLabelText("Título")).toHaveAttribute("aria-invalid", "true"))
      expect(document.activeElement).toBe(screen.getByLabelText("Título"))
      expect(onSubmit).not.toHaveBeenCalled()
    })

    it("título preenchido mas statement vazio foca o campo Statement", async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(<TwoFieldForm onSubmit={onSubmit} />)

      fireEvent.change(screen.getByLabelText("Título"), { target: { value: "minha query" } })
      fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

      await waitFor(() => expect(screen.getByLabelText("Statement")).toHaveAttribute("aria-invalid", "true"))
      expect(document.activeElement).toBe(screen.getByLabelText("Statement"))
    })

    it("formulário válido não mexe no foco (só submete)", async () => {
      const onSubmit = vi.fn().mockResolvedValue(undefined)
      render(<TwoFieldForm onSubmit={onSubmit} />)

      fireEvent.change(screen.getByLabelText("Título"), { target: { value: "minha query" } })
      fireEvent.change(screen.getByLabelText("Statement"), { target: { value: "SELECT 1" } })
      fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

      await waitFor(() => expect(onSubmit).toHaveBeenCalledWith({ title: "minha query", statement: "SELECT 1" }))
      expect(screen.getByLabelText("Título")).not.toHaveAttribute("aria-invalid")
      expect(screen.getByLabelText("Statement")).not.toHaveAttribute("aria-invalid")
    })
  })
})
