/**
 * EmailConfigForm — R2-8.3: o `<form>` tinha `noValidate` (desliga a
 * validação NATIVA do navegador) e o `useForm` não recebia `validate`
 * nenhum. O `required`/asterisco em host/porta/remetente era só cosmético:
 * submeter vazio ia direto pro `onSave` com string vazia / porta inválida,
 * sem aviso nenhum e sem foco nenhum.
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { EmailConfigForm } from "@/components/config/EmailConfigForm"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

function renderForm(onSave = vi.fn().mockResolvedValue(true)) {
  render(
    <EmailConfigForm
      config={null}
      recipients={[]}
      onSave={onSave}
      onAdd={vi.fn().mockResolvedValue(true)}
      onDelete={vi.fn().mockResolvedValue(true)}
      onTest={vi.fn().mockResolvedValue(true)}
    />,
  )
  return { onSave }
}

describe("EmailConfigForm — validação (R2-8.3)", () => {
  it("submeter com host/porta/remetente vazios NÃO chama onSave e foca o host (1º campo)", async () => {
    const { onSave } = renderForm()
    // Porta já vem com o default "25" — zera para forçar a invalidação dela também.
    fireEvent.change(screen.getByLabelText(/Porta/i), { target: { value: "" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar configurações/i }))

    await screen.findByText("Informe o host SMTP.")
    expect(screen.getByText("Informe uma porta válida (1-65535).")).toBeInTheDocument()
    expect(screen.getByText("Informe o remetente.")).toBeInTheDocument()
    expect(document.activeElement).toBe(screen.getByLabelText(/SMTP host/i))
    expect(onSave).not.toHaveBeenCalled()
  })

  it("porta fora do intervalo (0) bloqueia o submit e foca a Porta quando host/remetente já estão ok", async () => {
    const { onSave } = renderForm()
    fireEvent.change(screen.getByLabelText(/SMTP host/i), { target: { value: "smtp.example.com" } })
    fireEvent.change(screen.getByLabelText(/Remetente/i), { target: { value: "alerts@example.com" } })
    fireEvent.change(screen.getByLabelText(/Porta/i), { target: { value: "0" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar configurações/i }))

    await screen.findByText("Informe uma porta válida (1-65535).")
    expect(document.activeElement).toBe(screen.getByLabelText(/Porta/i))
    expect(onSave).not.toHaveBeenCalled()
  })

  it("formulário válido chama onSave com os valores certos e sem erro nenhum", async () => {
    const { onSave } = renderForm()
    fireEvent.change(screen.getByLabelText(/SMTP host/i), { target: { value: "smtp.example.com" } })
    fireEvent.change(screen.getByLabelText(/Remetente/i), { target: { value: "alerts@example.com" } })
    fireEvent.change(screen.getByLabelText(/Porta/i), { target: { value: "587" } })
    fireEvent.click(screen.getByRole("button", { name: /Salvar configurações/i }))

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ smtp_host: "smtp.example.com", smtp_port: 587, sender: "alerts@example.com" }),
      ),
    )
    expect(screen.queryByText("Informe o host SMTP.")).not.toBeInTheDocument()
  })
})
