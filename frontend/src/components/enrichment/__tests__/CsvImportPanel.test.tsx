/**
 * Importação de planilha para tabelas de enriquecimento.
 *
 * O parser tem testes próprios em `csv.test.ts`. Aqui o que se protege é o que
 * a TELA mostra antes de publicar, porque publicar substitui a versão inteira e
 * depois é tarde: quais linhas estão erradas (com o número da linha), o que sai
 * da tabela, e se o conteúdo cabe no teto.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import { CsvImportPanel } from "@/components/enrichment/CsvImportPanel"
import i18n from "@/i18n"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const CSV = [
  "cidr;site;criticidade",
  "10.0.0.0/16;matriz;media",
  "10.0.5.0/24;filial-sp;alta",
  "10.0.300.0/24;filial-bh;baixa",
].join("\n")

function mount(props: Partial<React.ComponentProps<typeof CsvImportPanel>> = {}) {
  const onChange = vi.fn()
  render(
    <CsvImportPanel matchMode="cidr" onChange={onChange} {...props} />,
  )
  return onChange
}

function cola(texto: string) {
  fireEvent.change(screen.getByLabelText(/Ou cole o conteúdo/i), {
    target: { value: texto },
  })
}

describe("CsvImportPanel", () => {
  it("detecta as colunas e entrega o corpo pronto ao formulário", async () => {
    const onChange = mount()
    cola(CSV)

    await waitFor(() => expect(onChange).toHaveBeenCalled())
    const ultimo = onChange.mock.calls[onChange.mock.calls.length - 1][0]
    expect(ultimo).toEqual({
      "10.0.0.0/16": { site: "matriz", criticidade: "media" },
      "10.0.5.0/24": { site: "filial-sp", criticidade: "alta" },
    })
    // A linha inválida NÃO entra no corpo, e a tela diz qual é.
    expect(Object.keys(ultimo)).not.toContain("10.0.300.0/24")
  })

  it("aponta a linha inválida pelo número dela no arquivo", async () => {
    mount()
    cola(CSV)

    // Contagem sem identificação não permite corrigir a planilha — era
    // exatamente o que o backend devolvia ("N linhas descartadas").
    const linha = await screen.findByTestId("csv-row-4")
    expect(linha).toHaveTextContent("10.0.300.0/24")
    expect(linha).toHaveTextContent(/chave inválida/i)
  })

  it("avisa quando a publicação REMOVE entradas, com exemplo", async () => {
    // Publicar substitui a versão inteira. Remoção em massa é o sintoma de ter
    // exportado o arquivo errado, e some sem aviso.
    mount({
      currentRows: {
        "10.0.0.0/16": { site: "matriz" },
        "10.0.7.0/24": { site: "some-daqui" },
        "10.0.8.0/24": { site: "some-tambem" },
      },
    })
    cola(CSV)

    expect(await screen.findByText(/Esta publicação remove entradas/i)).toBeInTheDocument()
    expect(screen.getByText(/10\.0\.7\.0\/24/)).toBeInTheDocument()
  })

  it("marca cada linha como nova, alterada ou inalterada", async () => {
    mount({
      currentRows: {
        "10.0.0.0/16": { site: "matriz", criticidade: "media" },
        "10.0.5.0/24": { site: "filial-sp", criticidade: "baixa" },
      },
    })
    cola(CSV)

    expect(await screen.findByTestId("csv-row-2")).toHaveTextContent(/inalterada/i)
    expect(screen.getByTestId("csv-row-3")).toHaveTextContent(/alterada/i)
  })

  it("não entrega corpo nenhum quando o conteúdo estoura o teto", async () => {
    // Estourar é recusa no servidor; deixar o botão de publicar ativo mandaria
    // o operador descobrir isso no envio.
    const onChange = mount({ maxBytes: 10 })
    cola(CSV)

    expect(await screen.findByText(/acima do teto por tabela/i)).toBeInTheDocument()
    await waitFor(() => {
      expect(onChange.mock.calls[onChange.mock.calls.length - 1][0]).toBeNull()
    })
  })

  it("deixar de marcar uma coluna a tira do corpo publicado", async () => {
    // Campo a mais ocupa memória residente no coletor, que tem teto por
    // processo, sem servir a nenhuma regra.
    const onChange = mount()
    cola(CSV)
    await waitFor(() => expect(onChange).toHaveBeenCalled())

    fireEvent.click(screen.getByLabelText("criticidade"))

    await waitFor(() => {
      const ultimo = onChange.mock.calls[onChange.mock.calls.length - 1][0]
      expect(ultimo["10.0.0.0/16"]).toEqual({ site: "matriz" })
    })
  })

  it("limpar o conteúdo devolve nulo em vez do último corpo válido", async () => {
    // Sem isto o botão de publicar seguiria armado com o corpo de uma seleção
    // anterior, e o operador publicaria o que achava ter descartado.
    const onChange = mount()
    cola(CSV)
    await waitFor(() => expect(onChange.mock.calls[onChange.mock.calls.length - 1][0]).not.toBeNull())

    cola("")
    await waitFor(() => expect(onChange.mock.calls[onChange.mock.calls.length - 1][0]).toBeNull())
  })

  it("não cobra formato de endereço quando o casamento é exato", async () => {
    mount({ matchMode: "exact" })
    cola("chave;dono\nservidor-01;infra")

    expect(await screen.findByTestId("csv-row-2")).not.toHaveTextContent(/inválida/i)
  })
})
