/**
 * useFirstInvalidFocus (R2-8.3) — generaliza o "failField" do NewUserModal.
 *
 * Cobre: marcar erro + campo + foco (`failField`), erro sem campo específico
 * (`failGeneral`), limpeza (`clearError`), `aria-invalid`/`aria-describedby`
 * só no campo culpado (`fieldErrorProps`), e o registro por CHAVE dinâmica
 * (`registerField`) — o motivo de existir este hook em vez do `useRef`
 * nomeado do NewUserModal original.
 */
import { render, screen, fireEvent } from "@testing-library/react"
import { useFirstInvalidFocus } from "@/hooks/useFirstInvalidFocus"

type Field = "username" | "password" | `dynamic_${string}`

const ERROR_ID = "harness-error"

function Harness({ dynamicKey = "dynamic_foo" }: { dynamicKey?: string }) {
  const { error, registerField, failField, failGeneral, clearError, fieldErrorProps } =
    useFirstInvalidFocus<Field>()

  return (
    <form
      onSubmit={(e) => {
        e.preventDefault()
        clearError()
      }}
    >
      {error && (
        <div id={ERROR_ID} role="alert">
          {error}
        </div>
      )}
      <input aria-label="username" ref={registerField("username")} {...fieldErrorProps("username", ERROR_ID)} />
      <input aria-label="password" ref={registerField("password")} {...fieldErrorProps("password", ERROR_ID)} />
      <input
        aria-label="dynamic"
        ref={registerField(dynamicKey as Field)}
        {...fieldErrorProps(dynamicKey as Field, ERROR_ID)}
      />
      <button type="button" onClick={() => failField("username", "usuário obrigatório")}>
        fail-username
      </button>
      <button type="button" onClick={() => failField("password", "senha obrigatória")}>
        fail-password
      </button>
      <button type="button" onClick={() => failField(dynamicKey as Field, "campo dinâmico obrigatório")}>
        fail-dynamic
      </button>
      <button type="button" onClick={() => failGeneral("falha de rede")}>
        fail-general
      </button>
      <button type="submit">limpar</button>
    </form>
  )
}

describe("useFirstInvalidFocus", () => {
  it("failField marca o erro, aponta o campo e move o foco para ele", () => {
    render(<Harness />)
    fireEvent.click(screen.getByText("fail-username"))

    expect(screen.getByRole("alert")).toHaveTextContent("usuário obrigatório")
    expect(document.activeElement).toBe(screen.getByLabelText("username"))
    expect(screen.getByLabelText("username")).toHaveAttribute("aria-invalid", "true")
    expect(screen.getByLabelText("username")).toHaveAttribute("aria-describedby", ERROR_ID)
    // Campo que NÃO falhou não fica marcado.
    expect(screen.getByLabelText("password")).not.toHaveAttribute("aria-invalid")
  })

  it("uma segunda falha troca o campo marcado e o foco", () => {
    render(<Harness />)
    fireEvent.click(screen.getByText("fail-username"))
    fireEvent.click(screen.getByText("fail-password"))

    expect(screen.getByRole("alert")).toHaveTextContent("senha obrigatória")
    expect(document.activeElement).toBe(screen.getByLabelText("password"))
    expect(screen.getByLabelText("password")).toHaveAttribute("aria-invalid", "true")
    expect(screen.getByLabelText("username")).not.toHaveAttribute("aria-invalid")
  })

  it("failGeneral marca o erro sem apontar nenhum campo (nenhum aria-invalid, foco intocado)", () => {
    render(<Harness />)
    screen.getByLabelText("password").focus()
    fireEvent.click(screen.getByText("fail-general"))

    expect(screen.getByRole("alert")).toHaveTextContent("falha de rede")
    expect(screen.getByLabelText("username")).not.toHaveAttribute("aria-invalid")
    expect(screen.getByLabelText("password")).not.toHaveAttribute("aria-invalid")
    // failGeneral não mexe no foco — é o caso "erro do backend no catch",
    // não uma validação de campo específico.
    expect(document.activeElement).toBe(screen.getByLabelText("password"))
  })

  it("clearError remove o banner e o aria-invalid", () => {
    render(<Harness />)
    fireEvent.click(screen.getByText("fail-username"))
    expect(screen.getByRole("alert")).toBeInTheDocument()

    fireEvent.click(screen.getByText("limpar"))

    expect(screen.queryByRole("alert")).not.toBeInTheDocument()
    expect(screen.getByLabelText("username")).not.toHaveAttribute("aria-invalid")
  })

  it("registerField aceita CHAVE DINÂMICA (campo de runtime, não fixo em compile-time)", () => {
    render(<Harness dynamicKey="dynamic_auth_token" />)
    fireEvent.click(screen.getByText("fail-dynamic"))

    expect(screen.getByRole("alert")).toHaveTextContent("campo dinâmico obrigatório")
    expect(document.activeElement).toBe(screen.getByLabelText("dynamic"))
    expect(screen.getByLabelText("dynamic")).toHaveAttribute("aria-invalid", "true")
  })
})
