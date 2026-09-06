/**
 * SyslogSourcesPanel — fontes do receptor nativo por integração push.
 * Prova: auto-oculta para pull; estado vazio avisa que 514 descarta tudo;
 * cadastro manda CIDR/stream/regras certos; o testador mostra o stream escolhido.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/services/api")

import * as api from "@/services/api"
import { SyslogSourcesPanel } from "../SyslogSourcesPanel"

const mocked = vi.mocked(api)

const info = (transport: string, streams: string[]): api.IngestInfo => ({
  integration_id: 7, platform: "fortinet_fortigate", transport, streams, has_token: true, endpoint_base: "/api/ingest", buffer_depth: 0, icon_id: null,
})

const source: api.SyslogSource = {
  id: 3, organization_id: 1, integration_id: 7, platform: "fortinet_fortigate", name: "fw-edge", source_cidr: "10.0.5.7/32",
  listen_port: null, transport: "any", default_stream: "traffic", classifier: { rules: [{ when: "@fortigate", stream: "traffic" }] },
  enabled: true, created_at: "2026-09-06T00:00:00Z", updated_at: "2026-09-06T00:00:00Z",
}

describe("SyslogSourcesPanel", () => {
  beforeEach(() => vi.clearAllMocks())

  it("não renderiza para integração pull", async () => {
    mocked.getIngestInfo.mockRejectedValue(new Error("422"))
    render(<SyslogSourcesPanel integrationId={7} canManage />)
    await waitFor(() => expect(mocked.getIngestInfo).toHaveBeenCalled())
    expect(screen.queryByTestId("syslog-sources")).toBeNull()
    expect(mocked.listSyslogSources).not.toHaveBeenCalled()
  })

  it("sem fontes: avisa que a porta descarta tudo, e lista quando há", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("push", ["traffic"]))
    mocked.listSyslogSources.mockResolvedValue([])
    const { unmount } = render(<SyslogSourcesPanel integrationId={7} canManage />)
    expect((await screen.findByTestId("syslog-sources")).textContent).toContain("Nenhuma fonte cadastrada")
    unmount()
    mocked.listSyslogSources.mockResolvedValue([source])
    render(<SyslogSourcesPanel integrationId={7} canManage />)
    const row = await screen.findByTestId("syslog-source-3")
    expect(row.textContent).toContain("fw-edge")
    expect(row.textContent).toContain("10.0.5.7/32")
    expect(row.textContent).toContain("qualquer")
  })

  it("cadastra a fonte com CIDR, stream padrão e regra de classificação", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("push", ["traffic"]))
    mocked.listSyslogSources.mockResolvedValue([])
    mocked.createSyslogSource.mockResolvedValue(source)
    render(<SyslogSourcesPanel integrationId={7} canManage />)
    await screen.findByTestId("syslog-sources")
    fireEvent.change(screen.getByTestId("syslog-name"), { target: { value: "fw-edge" } })
    expect(screen.getByTestId("syslog-create")).toBeDisabled() // sem CIDR
    fireEvent.change(screen.getByTestId("syslog-cidr"), { target: { value: "10.0.5.7/32" } })
    fireEvent.click(screen.getByTestId("syslog-add-rule"))
    fireEvent.change(screen.getByLabelText("when-0"), { target: { value: "@fortigate" } })
    expect(screen.getByTestId("syslog-create")).not.toBeDisabled()
    fireEvent.click(screen.getByTestId("syslog-create"))
    await waitFor(() => expect(mocked.createSyslogSource).toHaveBeenCalledTimes(1))
    expect(mocked.createSyslogSource).toHaveBeenCalledWith({
      integration_id: 7, name: "fw-edge", source_cidr: "10.0.5.7/32", listen_port: null, transport: "any",
      default_stream: "traffic", classifier: { rules: [{ when: "@fortigate", stream: "traffic" }] },
    })
    await screen.findByTestId("syslog-source-3")
    expect(screen.getByText(/Fonte fw-edge cadastrada/)).toBeInTheDocument()
  })

  it("testa uma linha e mostra o stream escolhido", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("push", ["traffic"]))
    mocked.listSyslogSources.mockResolvedValue([])
    mocked.testSyslogClassifier.mockResolvedValue({
      parsed: { format: "rfc3164", host: "fw01", app: null }, stream: "traffic", matched_rule: true, trace: [],
    })
    render(<SyslogSourcesPanel integrationId={7} canManage />)
    await screen.findByTestId("syslog-sources")
    fireEvent.change(screen.getByTestId("syslog-test-line"), { target: { value: '<134>Sep  6 01:02:03 fw01 devname="fw" logid="1"' } })
    fireEvent.click(screen.getByTestId("syslog-test-run"))
    const res = await screen.findByTestId("syslog-test-result")
    expect(res.textContent).toContain("traffic")
    expect(res.textContent).toContain("casou a regra")
    expect(res.textContent).toContain("rfc3164")
    expect(mocked.testSyslogClassifier).toHaveBeenCalledWith({ line: expect.stringContaining("devname"), classifier: undefined, default_stream: "traffic" })
  })
})
