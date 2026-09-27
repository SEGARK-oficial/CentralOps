/**
 * Testes de IdentityConfigForm — foco em SEC-07: `entra_post_login_redirect`
 * é usado pelo backend para redirecionar após o login SSO, então precisa ser
 * um path interno (sem virar open redirect).
 */
import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { IdentityConfigForm } from "@/components/config/IdentityConfigForm"
import * as api from "@/services/api"
import i18n from "@/i18n"

beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.spyOn(api, "getEntraSyncStatus").mockRejectedValue(new Error("not mocked"))

function renderForm(onSave = vi.fn().mockResolvedValue(true)) {
  render(
    <IdentityConfigForm
      config={null}
      loading={false}
      saving={false}
      testing={false}
      feedback={null}
      onSave={onSave}
      onTest={vi.fn().mockResolvedValue(true)}
    />,
  )
  return { onSave }
}

describe("IdentityConfigForm — SEC-07 (redirect interno)", () => {
  it("bloqueia salvar com redirect externo (protocol-relative) e mostra erro", async () => {
    const { onSave } = renderForm()
    const redirectInput = screen.getByLabelText("Redirect pós-login")
    fireEvent.change(redirectInput, { target: { value: "//evil.example.com" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar configuração/i }))

    expect(
      await screen.findByText(/Deve ser um caminho interno/i),
    ).toBeInTheDocument()
    expect(onSave).not.toHaveBeenCalled()
  })

  it("bloqueia salvar com URL absoluta de outro domínio", async () => {
    const { onSave } = renderForm()
    const redirectInput = screen.getByLabelText("Redirect pós-login")
    fireEvent.change(redirectInput, { target: { value: "https://evil.example.com/x" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar configuração/i }))

    expect(await screen.findByText(/Deve ser um caminho interno/i)).toBeInTheDocument()
    expect(onSave).not.toHaveBeenCalled()
  })

  it("aceita path interno válido e chama onSave", async () => {
    const { onSave } = renderForm()
    const redirectInput = screen.getByLabelText("Redirect pós-login")
    fireEvent.change(redirectInput, { target: { value: "/dashboard" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar configuração/i }))

    await waitFor(() =>
      expect(onSave).toHaveBeenCalledWith(
        expect.objectContaining({ entra_post_login_redirect: "/dashboard" }),
      ),
    )
  })

  it("erro some ao digitar de novo no campo", async () => {
    renderForm()
    const redirectInput = screen.getByLabelText("Redirect pós-login")
    fireEvent.change(redirectInput, { target: { value: "//evil.example.com" } })
    fireEvent.click(screen.getByRole("button", { name: /salvar configuração/i }))
    expect(await screen.findByText(/Deve ser um caminho interno/i)).toBeInTheDocument()

    fireEvent.change(redirectInput, { target: { value: "/ok" } })
    expect(screen.queryByText(/Deve ser um caminho interno/i)).not.toBeInTheDocument()
  })
})
