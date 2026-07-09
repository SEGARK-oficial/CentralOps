import { render, screen } from "@testing-library/react"
import AlertDetailsDrawer from "@/components/alerts/AlertDetailsDrawer"

describe("AlertDetailsDrawer", () => {
  it("mantem layout vertical com header fixo e corpo rolavel", () => {
    render(
      <AlertDetailsDrawer
        open
        onClose={vi.fn()}
        alert={{
          alert_id: "alert-1",
          title: "Suspicious login",
          severity: "high",
          platform: "wazuh",
          rule_groups: [],
          mitre_ids: [],
          mitre_tactics: [],
          mitre_techniques: [],
          agent_labels: {},
          data_fields: {},
          highlights: {},
          raw: {},
        }}
      />,
    )

    const dialog = screen.getByRole("dialog")
    const panel = dialog.querySelector(".shadow-2xl")
    const body = dialog.querySelector(".overflow-y-auto")

    expect(panel).not.toBeNull()
    expect(body).not.toBeNull()
    expect(panel).toHaveClass("flex-col")
    expect(panel).toHaveClass("overflow-hidden")
    expect(body).toHaveClass("min-h-0")
  })
})
