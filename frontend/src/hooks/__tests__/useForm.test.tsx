/**
 * useForm — cobre validação, submit e o `submitError` (antes o erro do
 * `onSubmit` rejeitado ia só pro `console.error` e o usuário ficava olhando
 * pro formulário sem nenhum sinal do que aconteceu).
 */
import { act, renderHook, waitFor } from "@testing-library/react"
import { useForm } from "@/hooks/useForm"

interface FormValues {
  name: string
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
})
