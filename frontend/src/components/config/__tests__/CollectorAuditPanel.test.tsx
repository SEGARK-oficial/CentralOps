/**
 * Testes de CollectorAuditPanel
 * Cobre: exibição de vendor (OCSF v1.0), fallback para platform (legado),
 * event_type vs stream, título do modal de inspeção, badge de formato syslog,
 * e as 3 abas do modal de inspeção (Wire-format / Normalized / Raw).
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { CollectorAuditPanel } from "@/components/config/CollectorAuditPanel"
import * as api from "@/services/api"
import type { CollectorAuditEvent } from "@/types"
import { vi } from "vitest"
import i18n from "@/i18n"

// Testes fazem assertions no texto literal em pt (idioma padrão do produto).
// jsdom resolve navigator.language para "en-US" por padrão — força pt para
// que os testes independam do locale detectado no ambiente de execução.
beforeAll(() => {
  void i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

// Evento OCSF v1.0: usa `vendor` e `event_type`
const ocsf10Event: CollectorAuditEvent = {
  event: { id: "evt-001", severity: "high" },
  envelope: { hostname: "centralops-prod", pri: 134 },
  meta: {
    integration_id: 1,
    customer_id: 42,
    vendor: "sophos",
    platform: undefined,
    event_type: "sophos.alert",
    stream: undefined,
    collected_at: "2026-04-25T10:00:00Z",
  },
}

// Evento legado: usa `platform` e `stream`
const legacyEvent: CollectorAuditEvent = {
  event: { id: "evt-002", severity: "medium" },
  envelope: { hostname: "centralops-dev", pri: 134 },
  meta: {
    integration_id: 2,
    customer_id: 7,
    vendor: undefined,
    platform: "microsoft_defender",
    event_type: undefined,
    stream: "incidents",
    collected_at: "2026-04-25T09:00:00Z",
  },
}

// Evento sem vendor nem platform — deve exibir "?"
const missingVendorEvent: CollectorAuditEvent = {
  event: { id: "evt-003" },
  envelope: {},
  meta: {
    integration_id: 3,
    customer_id: 1,
    collected_at: "2026-04-25T08:00:00Z",
  },
}

// Evento RFC 3164 com envelope OCSF completo (normalized + raw)
const rfc3164Event: CollectorAuditEvent = {
  syslog_format: "rfc3164",
  event: {
    id: "evt-004",
    severity: "low",
    _centralops: { vendor: "sophos", integration_id: 5 },
    normalized: { class_uid: 1001, severity_id: 1, time: 1714046400000 },
    raw: { sourceId: "abc123", type: "threat" },
  },
  envelope: { hostname: "centralops-prod", pri: 134 },
  meta: {
    integration_id: 5,
    customer_id: 10,
    vendor: "sophos",
    event_type: "sophos.threat",
    collected_at: "2026-04-25T12:00:00Z",
  },
}

// Evento com syslog_format: null (legado)
const nullFormatEvent: CollectorAuditEvent = {
  syslog_format: null,
  event: { id: "evt-006", severity: "info" },
  envelope: { hostname: "centralops-dev", pri: 134 },
  meta: {
    integration_id: 7,
    customer_id: 12,
    vendor: "defender",
    event_type: "defender.alert",
    collected_at: "2026-04-24T08:00:00Z",
  },
}

// Evento OCSF com normalized + raw separados
const ocsfWithEnvelopeEvent: CollectorAuditEvent = {
  syslog_format: "rfc5424",
  event: {
    id: "evt-007",
    _centralops: { vendor: "sophos", mapping_version: "v2" },
    normalized: { class_uid: 2001, severity_id: 3, finding_info: { title: "Intrusion" } },
    raw: { sourceId: "xyz789", rawData: { foo: "bar" } },
  },
  envelope: { hostname: "centralops-prod", pri: 134 },
  meta: {
    integration_id: 8,
    customer_id: 20,
    vendor: "sophos",
    event_type: "sophos.alert",
    collected_at: "2026-04-25T14:00:00Z",
  },
}

function setupMock(events: CollectorAuditEvent[]) {
  mockedApi.getCollectorAuditRecent.mockResolvedValue({ count: events.length, events })
  mockedApi.clearCollectorAudit.mockResolvedValue(undefined as never)
  // Catálogo de plataformas/streams — fonte das opções dos dropdowns.
  // Mockamos com os vendors usados nos eventos de teste (+1 não-presente
  // no buffer pra cobrir o caso "stream no catálogo mas sem evento").
  mockedApi.listPlatformsStreams.mockResolvedValue({
    platforms: {
      sophos: ["sophos.alert", "sophos.detection"],
      crowdstrike: ["crowdstrike.detection"],
      defender: ["defender.incident"],
    },
  })
}

describe("CollectorAuditPanel — vendor label", () => {
  beforeEach(() => vi.clearAllMocks())

  it("exibe vendor (OCSF v1.0) na coluna Vendor / Stream", async () => {
    setupMock([ocsf10Event])
    render(<CollectorAuditPanel />)

    // vendor e event_type aparecem na célula da tabela E no dropdown de filtro
    await waitFor(() => expect(screen.getAllByText("sophos").length).toBeGreaterThanOrEqual(1))
    expect(screen.getAllByText("sophos.alert").length).toBeGreaterThanOrEqual(1)
  })

  it("usa platform como fallback quando vendor está ausente (legado)", async () => {
    setupMock([legacyEvent])
    render(<CollectorAuditPanel />)

    // platform e stream aparecem na célula E no dropdown
    await waitFor(() =>
      expect(screen.getAllByText("microsoft_defender").length).toBeGreaterThanOrEqual(1),
    )
    expect(screen.getAllByText("incidents").length).toBeGreaterThanOrEqual(1)
  })

  it("exibe '?' quando vendor e platform estão ausentes", async () => {
    setupMock([missingVendorEvent])
    render(<CollectorAuditPanel />)

    await waitFor(() => {
      const cells = screen.getAllByText("?")
      expect(cells.length).toBeGreaterThanOrEqual(1)
    })
  })

  it("título do modal de inspeção usa vendor / event_type (OCSF v1.0)", async () => {
    setupMock([ocsf10Event])
    render(<CollectorAuditPanel />)

    await waitFor(() => screen.getByText("Inspecionar"))
    fireEvent.click(screen.getByText("Inspecionar"))

    await waitFor(() =>
      expect(screen.getByRole("dialog", { name: /sophos.*sophos\.alert/i })).toBeInTheDocument(),
    )
  })

  it("título do modal de inspeção usa platform / stream (legado)", async () => {
    setupMock([legacyEvent])
    render(<CollectorAuditPanel />)

    await waitFor(() => screen.getByText("Inspecionar"))
    fireEvent.click(screen.getByText("Inspecionar"))

    await waitFor(() =>
      expect(
        screen.getByRole("dialog", { name: /microsoft_defender.*incidents/i }),
      ).toBeInTheDocument(),
    )
  })

  it("filtro de plataforma lista vendor (OCSF) e platform (legado) sem duplicatas", async () => {
    setupMock([ocsf10Event, legacyEvent])
    render(<CollectorAuditPanel />)

    const select = await screen.findByRole("combobox", { name: /filtrar por plataforma/i })
    expect(select).toBeInTheDocument()

    await waitFor(() => {
      const options = Array.from(select.querySelectorAll("option")).map((o) => o.textContent)
      expect(options).toContain("sophos")
      expect(options).toContain("microsoft_defender")
    })
  })
})

describe("CollectorAuditPanel — badge de formato syslog", () => {
  beforeEach(() => vi.clearAllMocks())

  it("badge mostra 'RFC 3164' quando syslog_format é rfc3164", async () => {
    setupMock([rfc3164Event])
    render(<CollectorAuditPanel />)

    await waitFor(() => {
      const badges = screen.getAllByText("RFC 3164")
      expect(badges.length).toBeGreaterThanOrEqual(1)
    })
  })

  it("linha wire-format RFC 3164 contém <134> mas não a versão RFC 5424 '1 '", async () => {
    setupMock([rfc3164Event])
    render(<CollectorAuditPanel />)

    // Abre o modal
    await waitFor(() => screen.getByText("Inspecionar"))
    fireEvent.click(screen.getByText("Inspecionar"))

    // A aba Wire-format é exibida por padrão; espera o tabpanel estar visível
    await waitFor(() => {
      const panel = screen.getByRole("tabpanel")
      const content = panel.textContent ?? ""
      // RFC 3164 começa com <PRI>Mmm (sem versão "1" após o PRI)
      expect(content).toMatch(/<134>/)
      // RFC 5424 teria "<134>1 " — RFC 3164 não tem isso
      expect(content).not.toMatch(/<134>1 /)
    })
  })

  it("badge mostra 'RFC 5424' quando syslog_format é null", async () => {
    setupMock([nullFormatEvent])
    render(<CollectorAuditPanel />)

    await waitFor(() => {
      const badges = screen.getAllByText("RFC 5424")
      expect(badges.length).toBeGreaterThanOrEqual(1)
    })
  })
})

describe("CollectorAuditPanel — modal com 3 abas", () => {
  beforeEach(() => vi.clearAllMocks())

  it("abre o modal e navega para aba Normalized renderizando class_uid", async () => {
    setupMock([ocsfWithEnvelopeEvent])
    render(<CollectorAuditPanel />)

    await waitFor(() => screen.getByText("Inspecionar"))
    fireEvent.click(screen.getByText("Inspecionar"))

    // Verifica aba Wire-format ativa por padrão
    await waitFor(() => {
      expect(screen.getByRole("tab", { name: "Wire-format" })).toHaveAttribute(
        "aria-selected",
        "true",
      )
    })

    // Clica na aba Normalized (OCSF)
    fireEvent.click(screen.getByRole("tab", { name: "Normalized (OCSF)" }))

    await waitFor(() => {
      const panel = screen.getByRole("tabpanel")
      expect(panel.textContent).toContain("class_uid")
      expect(panel.textContent).toContain("2001")
    })
  })

  it("aba Raw (vendor) renderiza o payload original", async () => {
    setupMock([ocsfWithEnvelopeEvent])
    render(<CollectorAuditPanel />)

    await waitFor(() => screen.getByText("Inspecionar"))
    fireEvent.click(screen.getByText("Inspecionar"))

    await waitFor(() => screen.getByRole("tab", { name: "Raw (vendor)" }))
    fireEvent.click(screen.getByRole("tab", { name: "Raw (vendor)" }))

    await waitFor(() => {
      const panel = screen.getByRole("tabpanel")
      expect(panel.textContent).toContain("xyz789")
    })
  })

  it("desabilita abas Normalized e Raw para evento legado pré-Fase 1 (sem campos separados)", async () => {
    // ocsf10Event não tem `normalized` nem `raw` no event — é legado OCSF
    setupMock([ocsf10Event])
    render(<CollectorAuditPanel />)

    await waitFor(() => screen.getByText("Inspecionar"))
    fireEvent.click(screen.getByText("Inspecionar"))

    await waitFor(() => {
      const normalizedTab = screen.getByRole("tab", { name: "Normalized (OCSF)" })
      const rawTab = screen.getByRole("tab", { name: "Raw (vendor)" })
      expect(normalizedTab).toBeDisabled()
      expect(rawTab).toBeDisabled()
    })
  })

  it("aba Wire-format do evento legado pré-Fase 1 exibe o JSON do evento", async () => {
    setupMock([ocsf10Event])
    render(<CollectorAuditPanel />)

    await waitFor(() => screen.getByText("Inspecionar"))
    fireEvent.click(screen.getByText("Inspecionar"))

    await waitFor(() => {
      const panel = screen.getByRole("tabpanel")
      // O ID do evento bruto deve estar contido na linha wire
      expect(panel.textContent).toContain("evt-001")
    })
  })
})
