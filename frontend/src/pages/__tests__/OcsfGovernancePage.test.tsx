/**
 * Testes de componente — OcsfGovernancePage (governança OCSF).
 * Cobre: carga (policies + compliance), render dos dados estáveis (nomes/contagens),
 * empty-state, e o PUT de política via o Select customizado.
 * i18n não é inicializado nos testes → t() devolve a chave; asserções em DADOS.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { MemoryRouter } from "react-router-dom"
import OcsfGovernancePage from "@/pages/OcsfGovernancePage"
import * as api from "@/services/api"
import type { OcsfCompliance, OcsfPolicy } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const policies: OcsfPolicy[] = [
  { organization_id: 10, organization_name: "Org Alpha", enforcement_mode: "tag_and_pass", is_default: true },
  { organization_id: 20, organization_name: "Org Beta", enforcement_mode: "quarantine", is_default: false },
]

const compliance: OcsfCompliance = {
  validation_enabled: true,
  global_default: "tag_and_pass",
  ocsf_version: "1.8.0",
  items: [
    { integration_id: 1, integration_name: "FortiGate", organization_id: 10, enforcement_mode: "quarantine", invalid_quarantined_24h: 5 },
    { integration_id: 2, integration_name: "Okta", organization_id: 20, enforcement_mode: "tag_and_pass", invalid_quarantined_24h: 0 },
  ],
}

function renderPage() {
  return render(
    <MemoryRouter>
      <OcsfGovernancePage />
    </MemoryRouter>,
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mockedApi.listOcsfPolicies.mockResolvedValue(policies)
  mockedApi.getOcsfCompliance.mockResolvedValue(compliance)
})

it("loads and renders policies + compliance", async () => {
  renderPage()
  await waitFor(() => expect(screen.getByText("Org Alpha")).toBeInTheDocument())
  expect(screen.getByText("Org Beta")).toBeInTheDocument()
  // compliance rows + the invalid count badge (5)
  expect(screen.getByText("FortiGate")).toBeInTheDocument()
  expect(screen.getByText("5")).toBeInTheDocument()
  expect(mockedApi.listOcsfPolicies).toHaveBeenCalledTimes(1)
  expect(mockedApi.getOcsfCompliance).toHaveBeenCalledTimes(1)
})

it("shows an empty-state when there are no orgs and no integrations", async () => {
  mockedApi.listOcsfPolicies.mockResolvedValue([])
  mockedApi.getOcsfCompliance.mockResolvedValue({ ...compliance, items: [] })
  renderPage()
  // both cards render their empty state (keys are fine — assert the testids are absent)
  await waitFor(() => expect(mockedApi.listOcsfPolicies).toHaveBeenCalled())
  expect(screen.queryByTestId("ocsf-mode-10")).not.toBeInTheDocument()
})

it("PUTs the new enforcement mode when the org select changes", async () => {
  mockedApi.setOcsfPolicy.mockResolvedValue({
    ...policies[0], enforcement_mode: "quarantine", is_default: false,
  })
  renderPage()
  // custom Select: the trigger button carries the data-testid — open, then pick
  // the 2nd option (quarantine).
  const trigger = await screen.findByTestId("ocsf-mode-10")
  fireEvent.click(trigger)
  const options = await screen.findAllByRole("option")
  fireEvent.click(options[1])
  await waitFor(() =>
    expect(mockedApi.setOcsfPolicy).toHaveBeenCalledWith(10, "quarantine"),
  )
})
