/**
 * RouteForm tests — foco nas alavancas de redução de volume (ADR-0011).
 *
 * Cobre:
 * - Render padrão: fieldset "Redução de volume" visível para action="route", com o
 *   helper de cada campo dizendo quais valores são no-op (100 e 0).
 * - protect_detection vem TRUE por padrão (fail-safe) e os campos de
 *   amostragem/supressão começam desabilitados (no-op enquanto protegida).
 * - Desligar protect_detection é opt-out consciente: abre ConfirmDialog;
 *   cancelar mantém protegida; confirmar desliga e libera os campos + mostra
 *   aviso de risco.
 * - Religar protect_detection não pede confirmação (ação segura).
 * - Submissão envia protect_detection/sample_percent/suppress_* no payload.
 * - Fieldset de redução não aparece quando action="drop".
 * - A11y: checkbox de protect_detection e diálogo de confirmação acessíveis
 *   por role/name (navegáveis via teclado — Tab/Enter/Espaço).
 */

import { render, screen, fireEvent, waitFor, within } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { RouteForm } from "../RouteForm"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { Destination, Route } from "@/types"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const DEST_SPLUNK: Destination = {
  id: "dest-splunk",
  name: "Splunk HEC",
  kind: "splunk_hec",
  enabled: true,
  config: {},
  delivery: {},
  config_version: "1",
  organization_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  has_secret: true,
}

const ROUTE_BASE: Route = {
  id: "r-1",
  name: "Rota telemetria",
  priority: 100,
  condition: {},
  action: "route",
  destination_ids: ["dest-splunk"],
  is_final: true,
  canary_percent: 100,
  transform_ref: null,
  pii_redaction: null,
  enabled: true,
  organization_id: null,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
  unreachable: false,
  // protect_detection/sample_percent/suppress_* ausentes de propósito — a API
  // hoje não os retorna (gap de backend, ver types/index.ts). O form deve cair
  // nos defaults do modelo (protect_detection=true, sample_percent=100,
  // suppress_allow=0, suppress_window_s=30) quando ausentes.
}

beforeEach(() => {
  mockedApi.listDestinations.mockResolvedValue([DEST_SPLUNK])
})

describe("RouteForm — render padrão", () => {
  it("mostra o fieldset 'Redução de volume'", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    expect(screen.getByText("Redução de volume")).toBeInTheDocument()
  })

  // Regressão que sobreviveu à remoção do aviso: até a ADR-0015 o texto afirmava que as
  // flags REDUCTION_* nasciam DESLIGADAS. Nascem LIGADAS, e o portão real é o default
  // POR-ROTA — a versão errada fazia o operador ler "Evitado > 0" como bug. O aviso saiu
  // (falava em variável de ambiente e número de ADR), mas o fato que ele protegia mudou
  // de lugar, não de importância: agora vive no helper de cada campo. A guarda segue o fato.
  it("diz, no helper de cada campo, que 100 e 0 são os valores que não descartam nada", async () => {
    // Rota DESPROTEGIDA de propósito: com protect_detection=true (o default fail-safe)
    // o campo de amostragem fica desabilitado e o helper vira "Rota protegida nunca é
    // amostrada". O fato que este teste guarda só é visível com a proteção desligada.
    const UNPROTECTED: Route = { ...ROUTE_BASE, protect_detection: false }
    render(<RouteForm mode="edit" route={UNPROTECTED} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    expect(screen.getByText(/100 = sem amostragem/i)).toBeInTheDocument()
    expect(screen.getByText(/0 = supressão desligada/i)).toBeInTheDocument()
    // O erro original: atribuir o não-descarte a uma flag global em vez do default da rota.
    expect(document.body.textContent ?? "").not.toMatch(/desligadas por padrão/i)
  })

  it("não mostra o fieldset de redução quando action='drop'", async () => {
    const DROP_ROUTE: Route = { ...ROUTE_BASE, action: "drop", destination_ids: [] }
    render(<RouteForm mode="edit" route={DROP_ROUTE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())
    expect(screen.queryByText("Redução de volume")).not.toBeInTheDocument()
  })

  it("protect_detection vem marcado (true) por padrão e os campos de amostragem/supressão começam desabilitados", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    const protectCheckbox = screen.getByRole("checkbox", { name: /Proteger detecção/i })
    expect(protectCheckbox).toBeChecked()

    expect(screen.getByTestId("route-form-sample-percent")).toBeDisabled()
    expect(screen.getByTestId("route-form-suppress-key")).toBeDisabled()
    expect(screen.getByTestId("route-form-suppress-allow")).toBeDisabled()
    expect(screen.getByTestId("route-form-suppress-window")).toBeDisabled()

    // No estado protegido, nenhum aviso de risco é exibido.
    expect(screen.queryByText("Proteção de detecção desligada")).not.toBeInTheDocument()
  })
})

describe("RouteForm — opt-out consciente de protect_detection", () => {
  it("clicar para desligar abre o ConfirmDialog e NÃO desliga sem confirmação", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    const protectCheckbox = screen.getByRole("checkbox", { name: /Proteger detecção/i })
    fireEvent.click(protectCheckbox)

    // Ainda marcado — o estado só muda após confirmação explícita.
    expect(protectCheckbox).toBeChecked()
    expect(screen.getByText("Desligar proteção de detecção?")).toBeInTheDocument()
    expect(screen.getByText(/o que se perde não volta/i)).toBeInTheDocument()
  })

  it("cancelar o diálogo mantém a rota protegida", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    fireEvent.click(screen.getByRole("checkbox", { name: /Proteger detecção/i }))
    const dialog = screen.getByTestId("route-form-unprotect-dialog")
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancelar" }))

    await waitFor(() => expect(screen.queryByText("Desligar proteção de detecção?")).not.toBeInTheDocument())
    expect(screen.getByRole("checkbox", { name: /Proteger detecção/i })).toBeChecked()
    expect(screen.getByTestId("route-form-sample-percent")).toBeDisabled()
  })

  it("confirmar desliga a proteção, mostra o aviso de risco e libera os campos de redução", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    fireEvent.click(screen.getByRole("checkbox", { name: /Proteger detecção/i }))
    const dialog = screen.getByTestId("route-form-unprotect-dialog")
    fireEvent.click(within(dialog).getByTestId("route-form-unprotect-dialog-confirm"))

    await waitFor(() =>
      expect(screen.getByRole("checkbox", { name: /Proteger detecção/i })).not.toBeChecked(),
    )
    expect(screen.getByText("Proteção de detecção desligada")).toBeInTheDocument()
    expect(screen.getByTestId("route-form-sample-percent")).toBeEnabled()
    expect(screen.getByTestId("route-form-suppress-key")).toBeEnabled()
    expect(screen.getByTestId("route-form-suppress-allow")).toBeEnabled()
    expect(screen.getByTestId("route-form-suppress-window")).toBeEnabled()
  })

  it("religar protect_detection depois de desligada NÃO pede confirmação", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    const protectCheckbox = screen.getByRole("checkbox", { name: /Proteger detecção/i })
    fireEvent.click(protectCheckbox)
    fireEvent.click(
      within(screen.getByTestId("route-form-unprotect-dialog")).getByTestId(
        "route-form-unprotect-dialog-confirm",
      ),
    )
    await waitFor(() => expect(protectCheckbox).not.toBeChecked())

    // Religar: clique direto, sem diálogo.
    fireEvent.click(protectCheckbox)
    expect(protectCheckbox).toBeChecked()
    expect(screen.queryByText("Desligar proteção de detecção?")).not.toBeInTheDocument()
    expect(screen.getByTestId("route-form-sample-percent")).toBeDisabled()
  })
})

describe("RouteForm — submissão inclui os campos de redução", () => {
  it("envia protect_detection/sample_percent/suppress_* (defaults) quando a rota nunca foi editada", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={onSubmit} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    const [payload] = onSubmit.mock.calls[0]
    expect(payload).toMatchObject({
      protect_detection: true,
      sample_percent: 100,
      suppress_key: null,
      suppress_allow: 0,
      suppress_window_s: 30,
    })
  })

  it("envia os valores editados após desligar protect_detection e ajustar amostragem/supressão", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={onSubmit} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    fireEvent.click(screen.getByRole("checkbox", { name: /Proteger detecção/i }))
    fireEvent.click(
      within(screen.getByTestId("route-form-unprotect-dialog")).getByTestId(
        "route-form-unprotect-dialog-confirm",
      ),
    )
    await waitFor(() => expect(screen.getByTestId("route-form-sample-percent")).toBeEnabled())

    fireEvent.change(screen.getByTestId("route-form-sample-percent"), { target: { value: "25" } })
    fireEvent.change(screen.getByTestId("route-form-suppress-key"), { target: { value: "vendor,severity_id" } })
    fireEvent.change(screen.getByTestId("route-form-suppress-allow"), { target: { value: "5" } })
    fireEvent.change(screen.getByTestId("route-form-suppress-window"), { target: { value: "60" } })

    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    const [payload] = onSubmit.mock.calls[0]
    expect(payload).toMatchObject({
      protect_detection: false,
      sample_percent: 25,
      suppress_key: "vendor,severity_id",
      suppress_allow: 5,
      suppress_window_s: 60,
    })
  })

  // drop_raw é a alavanca de maior impacto isolado (o bruto costuma ser o maior
  // contribuinte de bytes do envelope) e é decisão POR-DESTINO.
  it("drop_raw nasce desligado e é bloqueado enquanto a rota protege detecção", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    const dropRaw = screen.getByTestId("route-form-drop-raw")
    expect(dropRaw).not.toBeChecked()
    expect(dropRaw).toBeDisabled()
  })

  it("envia drop_raw=true depois do opt-out de proteção", async () => {
    const onSubmit = vi.fn().mockResolvedValue(undefined)
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={onSubmit} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    fireEvent.click(screen.getByRole("checkbox", { name: /Proteger detecção/i }))
    fireEvent.click(
      within(screen.getByTestId("route-form-unprotect-dialog")).getByTestId(
        "route-form-unprotect-dialog-confirm",
      ),
    )
    await waitFor(() => expect(screen.getByTestId("route-form-drop-raw")).toBeEnabled())

    fireEvent.click(screen.getByTestId("route-form-drop-raw"))
    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

    await waitFor(() => expect(onSubmit).toHaveBeenCalledOnce())
    expect(onSubmit.mock.calls[0][0]).toMatchObject({ drop_raw: true })
  })
})

describe("RouteForm — a11y", () => {
  it("checkbox de protect_detection e o diálogo de confirmação são acessíveis por role/name", async () => {
    render(<RouteForm mode="edit" route={ROUTE_BASE} onCancel={vi.fn()} onSubmit={vi.fn()} />)
    await waitFor(() => expect(mockedApi.listDestinations).toHaveBeenCalled())

    const protectCheckbox = screen.getByRole("checkbox", { name: /Proteger detecção/i })
    expect(protectCheckbox).toBeInTheDocument()

    fireEvent.click(protectCheckbox)
    // O diálogo expõe título/descrição/ações por role — navegável via teclado
    // (Tab entre os botões, Enter/Espaço para ativar), sem depender de mouse.
    expect(screen.getByRole("heading", { name: "Desligar proteção de detecção?" })).toBeInTheDocument()
    const dialog = screen.getByTestId("route-form-unprotect-dialog")
    expect(within(dialog).getByRole("button", { name: "Cancelar" })).toBeInTheDocument()
    expect(within(dialog).getByRole("button", { name: "Desligar proteção" })).toBeInTheDocument()
  })
})
