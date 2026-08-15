/**
 * O campo de credencial precisa aparecer para destino que ACEITA segredo,
 * não só para o que exige.
 *
 * Defeito que estes testes travam: o webhook genérico monta header Bearer e
 * Basic corretamente no runtime, mas declarava a lista de segredos vazia. O
 * formulário decidia mostrar o input por `required_secrets.length > 0`, então
 * o campo nunca era desenhado e não existia lugar nenhum na tela para colar o
 * token. Quem escolhia Bearer salvava um destino que respondia 401 sem dizer
 * por quê, e a aba de credencial (que permitiria corrigir) só aparece quando
 * já existe credencial: um impasse fechado.
 */

import { render, screen, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeEach } from "vitest"
import { DestinationForm } from "@/components/destinations/DestinationForm"
import * as api from "@/services/api"
import type { DestinationType } from "@/types"

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

function tipo(over: Partial<DestinationType> = {}): DestinationType {
  return {
    kind: "webhook",
    label: "Generic Webhook",
    default_queue: "dispatch.webhook",
    capabilities: ["tls", "batch", "test"],
    required_secrets: [],
    config_schema: {
      type: "object",
      properties: {
        url: { type: "string", title: "Url" },
        auth_mode: {
          type: "string",
          title: "Auth Mode",
          enum: ["none", "bearer", "basic"],
          default: "none",
        },
      },
      required: ["url"],
    },
    delivery_schema: { type: "object", properties: {} },
    delivery_defaults: {},
    category: "Webhook",
    description: "Webhook HTTP genérico",
    icon_id: "webhook",
    docs_url: null,
    tier: "generic",
    order: 120,
    ...over,
  } as unknown as DestinationType
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("credencial de destino", () => {
  it("mostra o campo quando o segredo é apenas OPCIONAL", async () => {
    mockedApi.listDestinationTypes.mockResolvedValue([
      tipo({ optional_secrets: ["auth_token"] }),
    ])

    render(
      <DestinationForm
        mode="create"
        initialKind="webhook"
        onSubmit={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn()}
      />,
    )

    // Sem isto, o operador que escolhe Bearer não tem onde colar o token.
    expect(await screen.findByLabelText(/credencial/i)).toBeInTheDocument()
  })

  it("não marca o campo como obrigatório quando ele é opcional", async () => {
    mockedApi.listDestinationTypes.mockResolvedValue([
      tipo({ optional_secrets: ["auth_token"] }),
    ])

    render(
      <DestinationForm
        mode="create"
        initialKind="webhook"
        onSubmit={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn()}
      />,
    )

    const campo = await screen.findByLabelText(/credencial/i)
    // O asterisco é reservado para o que o backend recusa sem valor.
    expect(campo.getAttribute("aria-label") ?? campo.id).toBeDefined()
    await waitFor(() => {
      expect(screen.queryByText(/Credencial \(auth_token\) \*/)).not.toBeInTheDocument()
    })
  })

  it("continua mostrando o campo para destino que EXIGE segredo", async () => {
    mockedApi.listDestinationTypes.mockResolvedValue([
      tipo({ kind: "splunk_hec", label: "Splunk HEC", required_secrets: ["hec_token"] }),
    ])
    const kindSplunk = "splunk_hec"

    render(
      <DestinationForm
        mode="create"
        initialKind={kindSplunk}
        onSubmit={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn()}
      />,
    )

    expect(await screen.findByLabelText(/credencial/i)).toBeInTheDocument()
  })

  it("esconde o campo quando o destino não aceita segredo nenhum", async () => {
    mockedApi.listDestinationTypes.mockResolvedValue([
      tipo({ kind: "jsonl", label: "Arquivo JSONL", required_secrets: [], optional_secrets: [] }),
    ])
    const kindJsonl = "jsonl"

    render(
      <DestinationForm
        mode="create"
        initialKind={kindJsonl}
        onSubmit={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn()}
      />,
    )

    // Espera o formulário montar (o campo de config aparece) antes de negar.
    await screen.findByLabelText(/url/i)
    expect(screen.queryByLabelText(/credencial/i)).not.toBeInTheDocument()
  })

  it("o modo de autenticação vira lista, não caixa de texto", async () => {
    // Com `auth_mode` tipado como str o JSON Schema saía sem `enum`, a UI caía
    // no input de texto, e um typo como "Bearer" passava batido: o runtime não
    // reconhecia, não mandava header nenhum, e o destino respondia 401.
    mockedApi.listDestinationTypes.mockResolvedValue([
      tipo({ optional_secrets: ["auth_token"] }),
    ])

    render(
      <DestinationForm
        mode="create"
        initialKind="webhook"
        onSubmit={vi.fn().mockResolvedValue(undefined)}
        onCancel={vi.fn()}
      />,
    )

    const seletor = await screen.findByLabelText("Auth Mode")

    // O que importa é o operador ESCOLHER de uma lista, não digitar. O Select
    // do design system é um combobox custom, então a asserção é sobre o
    // comportamento e não sobre a tag.
    expect(seletor).not.toHaveAttribute("type", "text")
    expect(
      seletor.getAttribute("role") === "combobox" ||
        seletor.tagName.toLowerCase() === "select" ||
        seletor.tagName.toLowerCase() === "button",
    ).toBe(true)
  })
})
