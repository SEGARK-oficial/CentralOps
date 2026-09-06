/**
 * IngestSourcePanel — fonte genérica (custom_json): streams criados pelo operador.
 *
 * O que se prova: (1) o bloco de streams só existe para custom_json; (2) sem
 * streams a UI diz para criar o primeiro; (3) criar chama a API com o slug e a
 * classe, e o novo stream aparece nos badges/endpoint sem recarregar.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/services/api")

import * as api from "@/services/api"
import { IngestSourcePanel } from "../IngestSourcePanel"

const mocked = vi.mocked(api)

function info(platform: string, streams: string[]): api.IngestInfo {
  return {
    integration_id: 7,
    platform,
    transport: "push",
    streams,
    has_token: false,
    endpoint_base: "/api/ingest",
    buffer_depth: 0,
    icon_id: null,
  }
}

function renderPanel(platform: string) {
  return render(
    <MemoryRouter>
      <IngestSourcePanel integrationId={7} platform={platform} canManage />
    </MemoryRouter>,
  )
}

describe("IngestSourcePanel — custom_json streams", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("não mostra o bloco de streams para uma fonte push com stream fixo", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("fortinet_fortigate", ["traffic"]))
    renderPanel("fortinet_fortigate")
    await screen.findByText("traffic")
    expect(screen.queryByTestId("custom-streams")).toBeNull()
    expect(mocked.listCustomStreams).not.toHaveBeenCalled()
  })

  it("sem streams: avisa para criar o primeiro e o endpoint mostra o placeholder", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("custom_json", []))
    mocked.listCustomStreams.mockResolvedValue([])
    renderPanel("custom_json")
    const block = await screen.findByTestId("custom-streams")
    expect(block.textContent).toContain("Nenhum stream ainda")
    // O endpoint aparece no <code> e no snippet: basta o <code>.
    expect(screen.getAllByText(/POST .*\/api\/ingest\/<stream>/).some((el) => el.tagName === "CODE")).toBe(true)
    // Botão desabilitado até o nome ser um slug válido.
    expect(screen.getByTestId("custom-stream-create")).toBeDisabled()
  })

  it("cria o stream com slug + classe e reflete nos badges e no endpoint", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("custom_json", []))
    mocked.listCustomStreams.mockResolvedValue([])
    mocked.createCustomStream.mockResolvedValue({
      stream: "firewall-x",
      event_type: "custom_json.firewall-x",
      definition_id: "def-1",
      ocsf_class_uid: 0,
      ocsf_class_name: "Base Event",
      description: null,
      current_version_id: "v-1",
      endpoint: "/api/ingest/firewall-x",
    })
    renderPanel("custom_json")
    await screen.findByTestId("custom-streams")

    const name = screen.getByTestId("custom-stream-name")
    fireEvent.change(name, { target: { value: "Firewall X" } })
    expect(screen.getByTestId("custom-stream-create")).toBeDisabled() // maiúscula/espaço
    fireEvent.change(name, { target: { value: "firewall-x" } })
    expect(screen.getByTestId("custom-stream-create")).not.toBeDisabled()
    fireEvent.click(screen.getByTestId("custom-stream-create"))

    await waitFor(() => expect(mocked.createCustomStream).toHaveBeenCalledTimes(1))
    expect(mocked.createCustomStream).toHaveBeenCalledWith({ stream: "firewall-x", ocsf_class_uid: 0, description: undefined })
    // Aparece na lista, nos badges e vira o endpoint principal — sem recarregar.
    await screen.findByText("Stream firewall-x criado. O endpoint já aceita eventos.")
    expect(screen.getAllByText("firewall-x").length).toBeGreaterThanOrEqual(2)
    expect(screen.getAllByText(/POST .*\/api\/ingest\/firewall-x$/).some((el) => el.tagName === "CODE")).toBe(true)
    expect(screen.getByText("Abrir mapping")).toHaveAttribute("href", "/mappings/def-1")
  })
})
