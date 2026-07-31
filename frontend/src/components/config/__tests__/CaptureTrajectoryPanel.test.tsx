/**
 * A trajetória tem de ADMITIR o que não sabe.
 *
 * Um painel "antes/depois" sozinho mente por omissão: o mesmo evento aparece no
 * ring com três normalizações diferentes e nada as distingue. Estes testes
 * cobrem os três avisos que impedem o operador de tirar a conclusão errada —
 * bruto evictado, PII não redigida, e wire que não é o byte exato.
 */

import { render, screen, waitFor } from "@testing-library/react"
import { CaptureTrajectoryPanel } from "@/components/config/CaptureTrajectoryPanel"
import * as api from "@/services/api"
import i18n from "@/i18n"
import type { CaptureTrajectory } from "@/types"

vi.mock("@/services/api", async () => {
  const actual = await vi.importActual<typeof import("@/services/api")>("@/services/api")
  return { ...actual, getCaptureTrajectory: vi.fn() }
})
const mockedApi = vi.mocked(api)

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

beforeEach(() => {
  vi.clearAllMocks()
})

function traj(over: Partial<CaptureTrajectory> = {}): CaptureTrajectory {
  return {
    event_id: "e-1",
    session_id: "s-1",
    count: 2,
    complete: true,
    stages_present: ["collected", "delivered"],
    events: [
      {
        event: { raw: { msg: "cru do vendor" } },
        vendor: "sophos",
        captured_at: 1,
        outcome: "received",
        stage: "collected",
        payload_kind: "vendor_raw",
        pii_redacted: false,
        event_id: "e-1",
      },
      {
        event: { normalized: { class_uid: 1001 } },
        vendor: "sophos",
        captured_at: 2,
        outcome: "delivered",
        stage: "delivered",
        payload_kind: "envelope",
        pii_redacted: true,
        destination_kind: "splunk_hec",
        event_id: "e-1",
      },
    ],
    ...over,
  }
}

function renderPanel() {
  return render(<CaptureTrajectoryPanel sessionId="s-1" eventId="e-1" orgId={7} />)
}

it("renderiza um painel por estágio, em ordem de pipeline", async () => {
  mockedApi.getCaptureTrajectory.mockResolvedValue(traj())
  renderPanel()

  await waitFor(() => expect(screen.getByTestId("capture-trajectory")).toBeInTheDocument())
  expect(screen.getByTestId("trajectory-collected")).toBeInTheDocument()
  expect(screen.getByTestId("trajectory-delivered")).toBeInTheDocument()
  // O bruto do vendor aparece no estágio de coleta.
  expect(screen.getByText(/cru do vendor/)).toBeInTheDocument()
})

it("avisa quando o bruto saiu da janela do ring", async () => {
  // `collected` é a entrada mais VELHA do grupo e a primeira a ser podada. Sem
  // este aviso, a ausência do painel é lida como "o vendor não mandou nada".
  mockedApi.getCaptureTrajectory.mockResolvedValue(
    traj({ complete: false, stages_present: ["delivered"], count: 1 }),
  )
  renderPanel()

  await waitFor(() => expect(screen.getByTestId("trajectory-incomplete")).toBeInTheDocument())
  expect(screen.getByText(/saiu da janela do ring/i)).toBeInTheDocument()
})

it("marca o registro que NÃO passou pela redação de PII", async () => {
  // A redação é por ROTA e alcança o bloco `raw`: um evento dropado exibe em
  // claro o que o destino teria recebido redigido.
  mockedApi.getCaptureTrajectory.mockResolvedValue(traj())
  renderPanel()

  await waitFor(() => expect(screen.getByTestId("capture-trajectory")).toBeInTheDocument())
  const badges = screen.getAllByTestId("badge-pii-clear")
  // Só o registro `collected` tem pii_redacted=false.
  expect(badges).toHaveLength(1)
})

it("admite que com aggregate o evento individual não sai", async () => {
  mockedApi.getCaptureTrajectory.mockResolvedValue(
    traj({
      events: [
        {
          event: {},
          stage: "delivered",
          outcome: "delivered",
          payload_kind: "aggregate_metric",
          pii_redacted: true,
          event_id: "e-1",
        },
      ],
      stages_present: ["delivered"],
      count: 1,
    }),
  )
  renderPanel()

  await waitFor(() => expect(screen.getByTestId("badge-aggregate")).toBeInTheDocument())
})

it("mostra o wire com o nível de fidelidade e a nota do que falta", async () => {
  mockedApi.getCaptureTrajectory.mockResolvedValue(
    traj({
      events: [
        {
          event: {},
          stage: "delivered",
          outcome: "delivered",
          pii_redacted: true,
          event_id: "e-1",
          wire: {
            fidelity: "nondeterministic",
            encoding: "text",
            note: "timestamp e hostname são recalculados a cada envio",
            text: "<134>Jul 31 ...",
            bytes: 42,
            truncated: false,
          },
        },
      ],
      stages_present: ["delivered"],
      count: 1,
    }),
  )
  renderPanel()

  await waitFor(() => expect(screen.getByTestId("trajectory-wire")).toBeInTheDocument())
  expect(screen.getByText("Não determinístico")).toBeInTheDocument()
  // A nota é o que impede o operador de achar que o produto está errado quando
  // o diff contra o SIEM não bater.
  expect(screen.getByText(/recalculados a cada envio/)).toBeInTheDocument()
  expect(screen.getByText(/<134>Jul 31/)).toBeInTheDocument()
})

it("NÃO mostra prévia quando o destino grava o lote inteiro", async () => {
  // s3/security_lake gravam gzip/Parquet do LOTE. Um fragmento por evento
  // induziria exatamente a comparação errada.
  mockedApi.getCaptureTrajectory.mockResolvedValue(
    traj({
      events: [
        {
          event: {},
          stage: "delivered",
          outcome: "delivered",
          pii_redacted: true,
          event_id: "e-1",
          wire: {
            fidelity: "not_representable",
            encoding: "binary",
            note: "o objeto gravado é o LOTE inteiro em NDJSON comprimido",
          },
        },
      ],
      stages_present: ["delivered"],
      count: 1,
    }),
  )
  renderPanel()

  await waitFor(() => expect(screen.getByTestId("wire-no-preview")).toBeInTheDocument())
  expect(screen.getByText("Sem representação por evento")).toBeInTheDocument()
})

it("estado vazio quando o evento não está mais no ring", async () => {
  mockedApi.getCaptureTrajectory.mockResolvedValue(
    traj({ count: 0, events: [], stages_present: [], complete: false }),
  )
  renderPanel()

  await waitFor(() =>
    expect(screen.getByText(/Nenhum registro deste evento/i)).toBeInTheDocument(),
  )
})

it("erro de rede não quebra o modal", async () => {
  mockedApi.getCaptureTrajectory.mockRejectedValue(new Error("ECONNRESET"))
  renderPanel()

  await waitFor(() =>
    expect(screen.getByText("Erro ao carregar a trajetória")).toBeInTheDocument(),
  )
  expect(screen.getByText("ECONNRESET")).toBeInTheDocument()
})
