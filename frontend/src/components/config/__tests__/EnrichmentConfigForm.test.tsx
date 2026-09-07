/**
 * Aba Configuração › Enriquecimento.
 *
 * Dois grupos de teste, e o primeiro é o que justifica a tela existir:
 *
 * 1. **Contrato de três estados da senha.** Ausente MANTÉM, vazia REMOVE,
 *    preenchida substitui. O campo vem vazio em toda abertura, então mandar
 *    `redis_password: ""` por omissão apagaria a credencial de quem só quis
 *    trocar a porta — destruição de dado silenciosa, e o formulário nem pisca.
 * 2. **A consequência antes do jargão.** "Redis não configurado" não move
 *    ninguém; "VirusTotal e AbuseIPDB não rodam" move.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll, beforeEach } from "vitest"
import { EnrichmentConfigForm } from "@/components/config/EnrichmentConfigForm"
import * as api from "@/services/api"
import type { EnrichmentConfig } from "@/services/api"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

vi.mock("@/services/api")
const mockedApi = vi.mocked(api)

const base: EnrichmentConfig = {
  is_persisted: true,
  config_version: "abc123",
  enabled: true,
  redis_host: "redis-enrich",
  redis_port: 6379,
  redis_db: 0,
  redis_use_tls: false,
  redis_secret_configured: true,
  redis_url_masked: "redis://:***@redis-enrich:6379/0",
  redis_configured: true,
  remote_batch_budget_ms: 300,
  cycle_budget_ms: 30000,
  l1_max_entries: 10000,
  singleflight_wait_ms: 50,
  breaker_failure_threshold: 3,
  breaker_window_s: 600,
  breaker_cooldown_s: 120,
  breaker_max_cooldown_s: 1920,
  max_table_bytes: 33554432,
  lru_bytes: 67108864,
  remote_enrichers: ["abuseipdb", "virustotal"],
  propagation_worst_case_s: 35,
  geoip_dir: "/var/lib/centralops/geoip",
  geoip_files: [],
}

function mount(cfg: Partial<EnrichmentConfig> = {}) {
  const merged = { ...base, ...cfg }
  mockedApi.getEnrichmentConfig.mockResolvedValue(merged)
  mockedApi.updateEnrichmentConfig.mockResolvedValue(merged)
  return render(<EnrichmentConfigForm />)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe("EnrichmentConfigForm", () => {
  it("nomeia as fontes paradas quando o cache não está configurado", async () => {
    mount({ redis_configured: false, redis_host: null, redis_secret_configured: false })

    const aviso = await screen.findByText(/Enriquecimento por lote desligado/i)
    expect(aviso).toBeInTheDocument()
    // A consequência, com nome: é o que decide se o operador age agora.
    expect(screen.getByText(/abuseipdb, virustotal/i)).toBeInTheDocument()
  })

  it("não envia a senha quando o campo ficou vazio", async () => {
    // O caso comum: abrir a tela, mudar a porta, salvar. O campo de senha vem
    // vazio, e mandá-lo removeria a credencial gravada.
    mount()
    await screen.findByLabelText(/Endereço/i)

    fireEvent.change(screen.getByLabelText(/^Porta/i), { target: { value: "6380" } })
    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

    await waitFor(() => expect(mockedApi.updateEnrichmentConfig).toHaveBeenCalled())
    const payload = mockedApi.updateEnrichmentConfig.mock.calls[0][0]
    expect(payload.redis_port).toBe(6380)
    expect("redis_password" in payload).toBe(false)
  })

  it("envia a senha quando ela foi digitada", async () => {
    mount()
    await screen.findByLabelText(/Endereço/i)

    fireEvent.change(screen.getByLabelText(/^Senha/i), {
      target: { value: "nova-senha" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

    await waitFor(() => expect(mockedApi.updateEnrichmentConfig).toHaveBeenCalled())
    expect(mockedApi.updateEnrichmentConfig.mock.calls[0][0].redis_password).toBe(
      "nova-senha",
    )
  })

  it("testa com os valores do formulário, inclusive a senha ainda não salva", async () => {
    mount()
    await screen.findByLabelText(/Endereço/i)

    mockedApi.testEnrichmentRedis.mockResolvedValue({
      ok: true,
      message: "Conectado. PONG em 1.8 ms.",
      latency_ms: 1.8,
      maxmemory_policy: "allkeys-lru",
      maxmemory_bytes: 268435456,
      distinct_from_main: true,
      warnings: [],
    })

    fireEvent.change(screen.getByLabelText(/^Senha/i), { target: { value: "rascunho" } })
    fireEvent.click(screen.getByRole("button", { name: /Testar conexão/i }))

    await waitFor(() => expect(mockedApi.testEnrichmentRedis).toHaveBeenCalled())
    expect(mockedApi.testEnrichmentRedis.mock.calls[0][0].redis_password).toBe("rascunho")
    expect(await screen.findByText(/PONG em 1.8 ms/i)).toBeInTheDocument()
    expect(screen.getByText(/instância dedicada confirmada/i)).toBeInTheDocument()
    // Nada foi gravado por testar.
    expect(mockedApi.updateEnrichmentConfig).not.toHaveBeenCalled()
  })

  it("mostra que É a instância principal quando a sonda confirma a colisão", async () => {
    mount()
    await screen.findByLabelText(/Endereço/i)

    mockedApi.testEnrichmentRedis.mockResolvedValue({
      ok: false,
      message: "Conectou, mas é a MESMA instância do Redis principal.",
      distinct_from_main: false,
      warnings: [],
    })
    fireEvent.click(screen.getByRole("button", { name: /Testar conexão/i }))

    expect(
      await screen.findByText(/é a mesma instância do cache principal/i),
    ).toBeInTheDocument()
  })

  it("uma edição invalida o resultado da sonda anterior", async () => {
    // Sem isso, o "conectado" na tela passaria a se referir a um endereço que
    // não é mais o que está no formulário.
    mount()
    await screen.findByLabelText(/Endereço/i)

    mockedApi.testEnrichmentRedis.mockResolvedValue({
      ok: true,
      message: "Conectado. PONG em 1.8 ms.",
      warnings: [],
    })
    fireEvent.click(screen.getByRole("button", { name: /Testar conexão/i }))
    expect(await screen.findByText(/PONG em 1.8 ms/i)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText(/Endereço/i), {
      target: { value: "outro-host" },
    })
    expect(screen.queryByText(/PONG em 1.8 ms/i)).not.toBeInTheDocument()
  })

  it("converte MiB para bytes ao salvar os limites de tabela", async () => {
    mount()
    await screen.findByLabelText(/Endereço/i)

    fireEvent.change(screen.getByLabelText(/Máximo por tabela/i), {
      target: { value: "64" },
    })
    fireEvent.click(screen.getByRole("button", { name: "Salvar" }))

    await waitFor(() => expect(mockedApi.updateEnrichmentConfig).toHaveBeenCalled())
    expect(mockedApi.updateEnrichmentConfig.mock.calls[0][0].max_table_bytes).toBe(
      64 * 1024 * 1024,
    )
  })

  it("mostra o custo por processo do teto de tabela", async () => {
    // "32 MiB" parece o consumo do contêiner; são 8 processos.
    mount()
    expect(await screen.findByText(/32 MiB por processo × 8 processos/i)).toBeInTheDocument()
  })
})
