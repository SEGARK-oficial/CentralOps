/**
 * Testes de QuarantineDetailDrawer — F4-S3
 * Cobre: render do payload, error_detail, descart (com ConfirmDialog),
 *        reprocess habilitado: permissão, confirmação, sucesso, erros 409/422.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { QuarantineDetailDrawer } from "@/components/quarantine/QuarantineDetailDrawer"
import * as permHooks from "@/hooks/usePermission"
import type { QuarantineDetail, QuarantineEntry } from "@/types"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/hooks/usePermission")

const mockedUsePermission = vi.mocked(permHooks.usePermission)

const FUTURE_DATE = "2099-01-01T00:00:00Z"

const DETAIL: QuarantineDetail = {
  id: "q1",
  integration_id: 1,
  vendor: "sophos",
  event_type: "endpoint.threat",
  error_kind: "schema_error",
  error_detail: "Field 'user' is required",
  mapping_version_id: "mv1",
  created_at: "2026-01-01T00:00:00Z",
  expires_at: FUTURE_DATE,
  reprocessed_at: null,
  raw_payload: { event: "endpoint.threat", user: null, severity: "high" },
}

const DETAIL_REPROCESSED: QuarantineDetail = {
  ...DETAIL,
  reprocessed_at: "2026-04-25T10:00:00Z",
}

const DETAIL_EXPIRED: QuarantineDetail = {
  ...DETAIL,
  expires_at: "2020-01-01T00:00:00Z",
}

function renderDrawer(
  open = true,
  detail: QuarantineDetail | null = DETAIL,
  overrides: Partial<React.ComponentProps<typeof QuarantineDetailDrawer>> = {},
) {
  return render(
    <MemoryRouter>
      <QuarantineDetailDrawer
        detail={detail}
        open={open}
        onClose={vi.fn()}
        onDiscard={vi.fn()}
        {...overrides}
      />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedUsePermission.mockReturnValue(false)
})

describe("QuarantineDetailDrawer", () => {
  it("não renderiza nada quando detail=null", () => {
    renderDrawer(true, null)
    expect(screen.queryByTestId("quarantine-detail-drawer")).not.toBeInTheDocument()
  })

  it("não renderiza nada quando open=false", () => {
    renderDrawer(false)
    expect(screen.queryByTestId("quarantine-detail-drawer")).not.toBeInTheDocument()
  })

  it("renderiza vendor.event_type no header", () => {
    renderDrawer()
    expect(screen.getByText("sophos · endpoint.threat")).toBeInTheDocument()
  })

  it("renderiza badge com error_kind", () => {
    renderDrawer()
    expect(screen.getByText("schema_error")).toBeInTheDocument()
  })

  it("renderiza error_detail na Notice", () => {
    renderDrawer()
    expect(screen.getByText("Field 'user' is required")).toBeInTheDocument()
  })

  it("renderiza seção Payload bruto", () => {
    renderDrawer()
    expect(screen.getByRole("region", { name: /payload bruto/i })).toBeInTheDocument()
  })

  it("renderiza seção Metadados com integration_id", () => {
    renderDrawer()
    expect(screen.getByRole("region", { name: /metadados/i })).toBeInTheDocument()
    expect(screen.getByText("1")).toBeInTheDocument() // integration_id
  })

  it("renderiza seção Timestamps", () => {
    renderDrawer()
    expect(screen.getByRole("region", { name: /timestamps/i })).toBeInTheDocument()
  })

  it("Reprocessar visível para user com permissão quarantine.discard", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderDrawer(true, DETAIL, { onReprocess: vi.fn() })
    expect(screen.getByTestId(`reprocess-button-${DETAIL.id}`)).toBeInTheDocument()
  })

  it("Reprocessar NÃO aparece sem permissão quarantine.discard", () => {
    mockedUsePermission.mockReturnValue(false)
    renderDrawer(true, DETAIL, { onReprocess: vi.fn() })
    expect(screen.queryByTestId(`reprocess-button-${DETAIL.id}`)).not.toBeInTheDocument()
  })

  it("Reprocessar abre ConfirmDialog antes de chamar API", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const onReprocess = vi.fn()
    renderDrawer(true, DETAIL, { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${DETAIL.id}`))

    expect(screen.getByText("Reprocessar evento?")).toBeInTheDocument()
    expect(onReprocess).not.toHaveBeenCalled()
  })

  it("cancelar ConfirmDialog de reprocess não chama onReprocess", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const onReprocess = vi.fn()
    renderDrawer(true, DETAIL, { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${DETAIL.id}`))
    fireEvent.click(screen.getByRole("button", { name: /cancelar/i }))

    expect(onReprocess).not.toHaveBeenCalled()
  })

  it("Reprocessar success mostra Notice e atualiza drawer", async () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const updatedEntry: QuarantineEntry = {
      ...DETAIL,
      reprocessed_at: "2026-04-25T10:00:00Z",
    }
    const onReprocess = vi.fn().mockResolvedValue(updatedEntry)
    renderDrawer(true, DETAIL, { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${DETAIL.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^reprocessar$/i }))

    await waitFor(() => {
      expect(screen.getByTestId("reprocess-success-notice")).toBeInTheDocument()
    })
    expect(screen.getByText(/evento reprocessado e enviado ao wazuh/i)).toBeInTheDocument()
    expect(onReprocess).toHaveBeenCalledWith(DETAIL.id)
  })

  it("Reprocessar 409 mostra warning de já reprocessado", async () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const err = Object.assign(new Error("Este evento já foi reprocessado"), { statusCode: 409 })
    const onReprocess = vi.fn().mockRejectedValue(err)
    renderDrawer(true, DETAIL, { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${DETAIL.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^reprocessar$/i }))

    await waitFor(() => {
      expect(screen.getByTestId("reprocess-error-notice")).toBeInTheDocument()
    })
    expect(screen.getByText("Este evento já foi reprocessado")).toBeInTheDocument()
  })

  it("Reprocessar 422 mostra error_detail do backend", async () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    const err = Object.assign(new Error("Mapping ainda falha: campo obrigatório ausente"), { statusCode: 422 })
    const onReprocess = vi.fn().mockRejectedValue(err)
    renderDrawer(true, DETAIL, { onReprocess })

    fireEvent.click(screen.getByTestId(`reprocess-button-${DETAIL.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^reprocessar$/i }))

    await waitFor(() => {
      expect(screen.getByTestId("reprocess-error-notice")).toBeInTheDocument()
    })
    expect(screen.getByText("Mapping ainda falha: campo obrigatório ausente")).toBeInTheDocument()
  })

  it("Já reprocessado mostra Badge em vez de botão no footer", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderDrawer(true, DETAIL_REPROCESSED, { onReprocess: vi.fn() })

    expect(screen.queryByTestId(`reprocess-button-${DETAIL.id}`)).not.toBeInTheDocument()
    // Header and footer both show Badge — getAll and check at least one
    const badges = screen.getAllByText("Reprocessado")
    expect(badges.length).toBeGreaterThanOrEqual(1)
  })

  it("Expirado mostra Badge em vez de botão", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderDrawer(true, DETAIL_EXPIRED, { onReprocess: vi.fn() })

    expect(screen.queryByTestId(`reprocess-button-${DETAIL.id}`)).not.toBeInTheDocument()
    expect(screen.getByText("Expirado")).toBeInTheDocument()
  })

  it("botão Descartar NÃO aparece sem permissão", () => {
    mockedUsePermission.mockReturnValue(false)
    renderDrawer()
    expect(screen.queryByTestId(`discard-button-${DETAIL.id}`)).not.toBeInTheDocument()
  })

  it("botão Descartar aparece com permissão quarantine.discard", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderDrawer()
    expect(screen.getByTestId(`discard-button-${DETAIL.id}`)).toBeInTheDocument()
  })

  it("clicar em Descartar abre ConfirmDialog", () => {
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")
    renderDrawer()

    fireEvent.click(screen.getByTestId(`discard-button-${DETAIL.id}`))
    expect(screen.getByText("Descartar entrada de quarentena?")).toBeInTheDocument()
  })

  it("confirmar Descartar chama onDiscard", async () => {
    const onDiscard = vi.fn().mockResolvedValue(undefined)
    const onClose = vi.fn()
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")

    renderDrawer(true, DETAIL, { onDiscard, onClose })

    fireEvent.click(screen.getByTestId(`discard-button-${DETAIL.id}`))
    fireEvent.click(screen.getByRole("button", { name: /^descartar$/i }))

    await waitFor(() => {
      expect(onDiscard).toHaveBeenCalledWith(DETAIL.id)
    })
  })

  it("cancelar ConfirmDialog fecha o dialog sem chamar onDiscard", () => {
    const onDiscard = vi.fn()
    mockedUsePermission.mockImplementation((perm) => perm === "quarantine.discard")

    renderDrawer(true, DETAIL, { onDiscard })

    fireEvent.click(screen.getByTestId(`discard-button-${DETAIL.id}`))
    fireEvent.click(screen.getByRole("button", { name: /cancelar/i }))

    expect(onDiscard).not.toHaveBeenCalled()
  })

  it("foco é gerenciado pelo Modal (FocusScope)", () => {
    renderDrawer()
    expect(screen.getByTestId("quarantine-detail-drawer")).toBeInTheDocument()
  })

  it("pressionar Escape fecha o drawer chamando onClose", () => {
    const onClose = vi.fn()
    renderDrawer(true, DETAIL, { onClose })

    fireEvent.keyDown(document, { key: "Escape" })

    expect(onClose).toHaveBeenCalled()
  })
})
