/**
 * IngestSourcePanel — AWS via Firehose: uma URL de destino por stream e o token
 * entra como "access key" do Firehose (não como Bearer de edge-collector).
 */
import { render, screen } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/services/api")

import * as api from "@/services/api"
import { IngestSourcePanel } from "../IngestSourcePanel"

const mocked = vi.mocked(api)

function info(platform: string, streams: string[]): api.IngestInfo {
  return {
    integration_id: 9,
    platform,
    transport: "push",
    streams,
    has_token: false,
    endpoint_base: "/api/ingest",
    buffer_depth: 0,
    icon_id: "aws",
  }
}

function renderPanel(platform: string) {
  return render(
    <MemoryRouter>
      <IngestSourcePanel integrationId={9} platform={platform} canManage />
    </MemoryRouter>,
  )
}

describe("IngestSourcePanel — aws_firehose", () => {
  beforeEach(() => {
    vi.clearAllMocks()
  })

  it("lista uma URL /firehose/<stream> por stream e o snippet usa AccessKey", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("aws_firehose", ["cloudtrail", "waf"]))
    renderPanel("aws_firehose")
    const block = await screen.findByTestId("firehose-endpoints")
    const codes = Array.from(block.querySelectorAll("code")).map((c) => c.textContent)
    expect(codes).toEqual([
      `${window.location.origin}/api/ingest/firehose/cloudtrail`,
      `${window.location.origin}/api/ingest/firehose/waf`,
    ])
    // Nada de "POST /api/ingest/<stream>" genérico para o Firehose.
    expect(screen.queryByText(/^POST /)).toBeNull()
    const snippet = screen.getByText(/create-delivery-stream/)
    expect(snippet.textContent).toContain('"AccessKey": "<SEU_TOKEN_DE_INGESTAO>"')
    expect(snippet.textContent).toContain("/api/ingest/firehose/waf")
    expect(snippet.textContent).not.toContain("Authorization")
  })

  it("outra fonte push continua com o endpoint genérico", async () => {
    mocked.getIngestInfo.mockResolvedValue(info("fortinet_fortigate", ["traffic"]))
    renderPanel("fortinet_fortigate")
    await screen.findByText("traffic")
    expect(screen.queryByTestId("firehose-endpoints")).toBeNull()
  })
})
