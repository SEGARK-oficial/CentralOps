/**
 * CollectionFiltersSection — a seção é 100% plugin-driven.
 *
 * Os schemas abaixo são INVENTADOS de propósito (vendor "acme", stream
 * "widgets"): se algum teste passasse por conhecer Wazuh, a seção não seria
 * plugin-driven. O que se prova aqui:
 *  - os três `type` declaráveis (`int_range`, `enum`, `bool`) renderizam o
 *    controle certo a partir do schema, sem nenhum ramo por vendor;
 *  - `warning_text` está na tela ANTES de ligar;
 *  - ligar (sair do default) exige confirmação; cancelar não altera nada;
 *  - desligar volta ao default em UM clique e some do payload;
 *  - o valor default nunca é persistido — o estado emitido só carrega o que de
 *    fato filtra.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react"
import { describe, it, expect, vi, beforeAll } from "vitest"
import i18n from "@/i18n"
import {
  CollectionFiltersSection,
  countActiveFilters,
  serializeFilters,
  type CollectionFilterValues,
} from "@/components/integrations/CollectionFiltersSection"
import type { CollectionFilterFieldRead } from "@/types"

beforeAll(async () => {
  await i18n.changeLanguage("pt")
})

const RANGE_FIELD: CollectionFilterFieldRead = {
  key: "min_widget_level",
  label: "Nível mínimo do widget",
  type: "int_range",
  default: 0,
  min: 0,
  max: 16,
  help_text: "Só coleta widgets neste nível ou acima.",
  warning_text: "O que for filtrado aqui NUNCA entra na plataforma.",
}

const ENUM_FIELD: CollectionFilterFieldRead = {
  key: "severity",
  label: "Severidade mínima",
  type: "enum",
  default: "all",
  options: ["all", "medium", "high"],
  help_text: "Corta abaixo da severidade escolhida.",
  warning_text: "Eventos abaixo do corte não são transportados.",
}

const BOOL_FIELD: CollectionFilterFieldRead = {
  key: "skip_heartbeats",
  label: "Ignorar heartbeats",
  type: "bool",
  default: false,
  help_text: "Descarta eventos de heartbeat na consulta.",
  warning_text: "Heartbeats deixam de existir na plataforma.",
}

const AVAILABLE = { widgets: [RANGE_FIELD, ENUM_FIELD, BOOL_FIELD] }

function renderSection(values: CollectionFilterValues = {}) {
  const onChange = vi.fn()
  const utils = render(
    <CollectionFiltersSection availableFilters={AVAILABLE} values={values} onChange={onChange} />,
  )
  return { onChange, ...utils }
}

/** Confirma o diálogo de gravidade (o botão vive no ConfirmDialog). */
async function confirmDialog() {
  const confirm = await screen.findByTestId("collection-filters-confirm-dialog-confirm")
  fireEvent.click(confirm)
}

describe("CollectionFiltersSection — renderização dinâmica pelos 3 tipos", () => {
  it("int_range vira campo numérico com min/max do schema", () => {
    renderSection()
    const input = screen.getByTestId("collection-filter-input-min_widget_level")
    expect(input).toHaveAttribute("type", "number")
    expect(input).toHaveAttribute("min", "0")
    expect(input).toHaveAttribute("max", "16")
    expect(input).toHaveValue(0)
  })

  it("enum vira select com exatamente as options declaradas", () => {
    renderSection()
    const select = screen.getByTestId("collection-filter-input-severity")
    expect(select.tagName).toBe("SELECT")
    const options = Array.from(select.querySelectorAll("option")).map((o) => o.getAttribute("value"))
    expect(options).toEqual(["all", "medium", "high"])
    // O default é marcado como "sem filtro" na própria lista.
    expect(screen.getByText("all — sem filtro")).toBeInTheDocument()
  })

  it("bool vira checkbox desmarcado quando o default é false", () => {
    renderSection()
    const checkbox = screen.getByTestId("collection-filter-input-skip_heartbeats")
    expect(checkbox).toHaveAttribute("type", "checkbox")
    expect(checkbox).not.toBeChecked()
  })

  it("não renderiza nada quando a plataforma não declara filtros", () => {
    const { container } = render(
      <CollectionFiltersSection availableFilters={{}} values={{}} onChange={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it("stream com lista vazia de campos não vira seção", () => {
    const { container } = render(
      <CollectionFiltersSection availableFilters={{ widgets: [] }} values={{}} onChange={vi.fn()} />,
    )
    expect(container).toBeEmptyDOMElement()
  })

  it("agrupa os campos pelo stream e mostra o help_text de cada um", () => {
    renderSection()
    expect(screen.getByText("Stream: widgets")).toBeInTheDocument()
    expect(screen.getByText("Só coleta widgets neste nível ou acima.")).toBeInTheDocument()
    expect(screen.getByText("Corta abaixo da severidade escolhida.")).toBeInTheDocument()
    expect(screen.getByText("Descarta eventos de heartbeat na consulta.")).toBeInTheDocument()
  })
})

describe("CollectionFiltersSection — consequência antes da ação", () => {
  it("o warning_text está na tela ANTES de ligar o filtro", () => {
    renderSection()
    // Nada ligado ainda…
    expect(screen.getByTestId("collection-filter-state-min_widget_level")).toHaveTextContent(
      "Sem filtro — coleta tudo",
    )
    // …e o aviso do plugin já está visível.
    expect(screen.getByTestId("collection-filter-warning-min_widget_level")).toHaveTextContent(
      "O que for filtrado aqui NUNCA entra na plataforma.",
    )
  })

  it("deixa explícito que não é retroativo", () => {
    renderSection()
    expect(screen.getByText(/não reprocessa o que já passou/i)).toBeInTheDocument()
    expect(screen.getByText(/não recupera o que ficou de fora/i)).toBeInTheDocument()
  })

  it("'Sem filtro' é o estado anunciado quando nada está configurado", () => {
    renderSection()
    expect(screen.getByTestId("collection-filters-none-badge")).toHaveTextContent("Sem filtro")
    expect(screen.queryByTestId("collection-filters-active-badge")).not.toBeInTheDocument()
  })
})

describe("CollectionFiltersSection — ligar exige confirmação consciente", () => {
  it("int_range: digitar e sair do campo abre o diálogo e só aplica após confirmar", async () => {
    const { onChange } = renderSection()
    const input = screen.getByTestId("collection-filter-input-min_widget_level")
    fireEvent.change(input, { target: { value: "7" } })
    fireEvent.blur(input)

    // Ainda NÃO aplicou.
    expect(onChange).not.toHaveBeenCalled()
    expect(await screen.findByText("Ligar este filtro de coleta?")).toBeInTheDocument()

    await confirmDialog()
    await waitFor(() => expect(onChange).toHaveBeenCalledTimes(1))
    expect(onChange.mock.calls[0][0]).toEqual({ widgets: { min_widget_level: 7 } })
  })

  it("cancelar o diálogo não altera nada", async () => {
    const { onChange } = renderSection()
    const input = screen.getByTestId("collection-filter-input-min_widget_level")
    fireEvent.change(input, { target: { value: "12" } })
    fireEvent.blur(input)
    await screen.findByText("Ligar este filtro de coleta?")

    fireEvent.click(screen.getByRole("button", { name: /Cancelar/i }))
    await waitFor(() =>
      expect(screen.queryByText("Ligar este filtro de coleta?")).not.toBeInTheDocument(),
    )
    expect(onChange).not.toHaveBeenCalled()
    // E o campo volta a mostrar o valor que está de fato valendo.
    expect(screen.getByTestId("collection-filter-input-min_widget_level")).toHaveValue(0)
  })

  it("enum: escolher opção fora do default pede confirmação", async () => {
    const { onChange } = renderSection()
    fireEvent.change(screen.getByTestId("collection-filter-input-severity"), {
      target: { value: "high" },
    })
    expect(onChange).not.toHaveBeenCalled()
    await confirmDialog()
    expect(onChange.mock.calls[0][0]).toEqual({ widgets: { severity: "high" } })
  })

  it("bool: marcar o checkbox pede confirmação", async () => {
    const { onChange } = renderSection()
    fireEvent.click(screen.getByTestId("collection-filter-input-skip_heartbeats"))
    expect(onChange).not.toHaveBeenCalled()
    await confirmDialog()
    expect(onChange.mock.calls[0][0]).toEqual({ widgets: { skip_heartbeats: true } })
  })

  it("o diálogo repete o aviso do plugin e o valor que passará a valer", async () => {
    renderSection()
    fireEvent.change(screen.getByTestId("collection-filter-input-severity"), {
      target: { value: "medium" },
    })
    const dialog = await screen.findByTestId("collection-filters-confirm-dialog")
    expect(dialog).toHaveTextContent("Eventos abaixo do corte não são transportados.")
    expect(dialog).toHaveTextContent("Severidade mínima: medium")
  })

  it("recusa valor fora da faixa declarada sem chamar o backend", () => {
    const { onChange } = renderSection()
    const input = screen.getByTestId("collection-filter-input-min_widget_level")
    fireEvent.change(input, { target: { value: "99" } })
    fireEvent.blur(input)
    expect(screen.getByText("Informe um número inteiro entre 0 e 16.")).toBeInTheDocument()
    expect(onChange).not.toHaveBeenCalled()
    expect(screen.queryByText("Ligar este filtro de coleta?")).not.toBeInTheDocument()
  })
})

describe("CollectionFiltersSection — desligar é um clique, sem confirmação", () => {
  const ACTIVE: CollectionFilterValues = { widgets: { min_widget_level: 7, severity: "high" } }

  it("mostra o resumo de filtros ativos", () => {
    renderSection(ACTIVE)
    expect(screen.getByTestId("collection-filters-active-badge")).toHaveTextContent("2 filtros ativos")
    expect(screen.getByTestId("collection-filter-state-min_widget_level")).toHaveTextContent(
      "Filtrando na origem",
    )
  })

  it("'Remover filtro' volta ao default em um clique e some do payload", () => {
    const { onChange } = renderSection(ACTIVE)
    fireEvent.click(screen.getByTestId("collection-filter-reset-min_widget_level"))
    expect(screen.queryByText("Ligar este filtro de coleta?")).not.toBeInTheDocument()
    // O default NÃO é persistido: a chave desaparece.
    expect(onChange).toHaveBeenCalledWith({ widgets: { severity: "high" } })
  })

  it("'Remover todos os filtros' zera o stream inteiro", () => {
    const { onChange } = renderSection(ACTIVE)
    fireEvent.click(screen.getByTestId("collection-filters-reset-all"))
    expect(onChange).toHaveBeenCalledWith({})
  })

  it("desmarcar um bool ligado não pede confirmação", () => {
    const { onChange } = renderSection({ widgets: { skip_heartbeats: true } })
    fireEvent.click(screen.getByTestId("collection-filter-input-skip_heartbeats"))
    expect(screen.queryByText("Ligar este filtro de coleta?")).not.toBeInTheDocument()
    // Stream sem nenhum filtro some do payload (o backend grava NULL, não {}).
    expect(onChange).toHaveBeenCalledWith({})
  })

  it("botão de remover só existe no campo que está filtrando", () => {
    renderSection({ widgets: { severity: "high" } })
    expect(screen.getByTestId("collection-filter-reset-severity")).toBeInTheDocument()
    expect(screen.queryByTestId("collection-filter-reset-min_widget_level")).not.toBeInTheDocument()
  })
})

describe("CollectionFiltersSection — helpers", () => {
  it("countActiveFilters conta por stream", () => {
    expect(countActiveFilters({})).toBe(0)
    expect(countActiveFilters({ a: { x: 1 }, b: { y: 2, z: true } })).toBe(3)
  })

  it("serializeFilters ignora ordem de chave (senão todo PUT viraria auditoria falsa)", () => {
    expect(serializeFilters({ b: { y: 1, x: 2 }, a: { k: 3 } })).toBe(
      serializeFilters({ a: { k: 3 }, b: { x: 2, y: 1 } }),
    )
    expect(serializeFilters({ a: { k: 3 } })).not.toBe(serializeFilters({ a: { k: 4 } }))
  })
})
