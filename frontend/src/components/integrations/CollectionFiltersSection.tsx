import type React from "react"
import { useState } from "react"
import { useTranslation } from "react-i18next"
import { AlertTriangleIcon, FilterIcon, RotateCcwIcon } from "lucide-react"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Notice } from "@/components/ui/Notice/Notice"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import type { CollectionFilterFieldRead, CollectionFilterValue } from "@/types"

/**
 * Filtros de COLETA — descarte empurrado para a consulta do fornecedor.
 *
 * 100% plugin-driven: tudo o que esta tela desenha (label, tipo, faixa, opções,
 * texto de ajuda, texto de aviso, e qual valor significa "não filtra nada") vem
 * do schema que o vendor declara no backend. Não existe `switch (platform)`
 * aqui — vendor novo que passe a declarar filtros aparece renderizado sozinho, e
 * plataforma que não declara nenhum simplesmente não tem esta seção.
 *
 * Por que a UX é pesada de propósito: o evento filtrado aqui NUNCA entra na
 * plataforma. Não aparece na captura ao vivo, não gera campo novo no Drift
 * Explorer e não fica disponível para uma regra de roteamento futura. E não é
 * retroativo em nenhuma direção — ligar não reprocessa o passado, desligar não
 * recupera o que foi pulado. Por isso: o aviso do plugin fica visível ANTES de
 * ligar, ligar exige confirmação explícita (mesmo padrão de gravidade do
 * "Proteger detecção" em RouteForm) e desligar é sempre um clique só.
 */

/** `{stream: {chave: valor}}` — espelha o que o backend persiste. */
export type CollectionFilterValues = Record<string, Record<string, CollectionFilterValue>>

/** `true` quando o valor equivale a "não filtra nada". */
export function isFilterNoop(
  field: CollectionFilterFieldRead,
  value: CollectionFilterValue | undefined,
): boolean {
  return value === undefined || value === null || value === field.default
}

/** Valor em vigor: o gravado, ou o default do plugin (que nunca filtra). */
export function effectiveFilterValue(
  field: CollectionFilterFieldRead,
  values: CollectionFilterValues,
  stream: string,
): CollectionFilterValue | null {
  const stored = values[stream]?.[field.key]
  return stored === undefined ? field.default : stored
}

/** Quantos filtros estão de fato descartando dado. */
export function countActiveFilters(values: CollectionFilterValues): number {
  return Object.values(values).reduce((total, byKey) => total + Object.keys(byKey).length, 0)
}

/**
 * Serialização estável para comparar "o que está na tela" com "o que está
 * gravado". Sem ordenar as chaves, reordenação de inserção contaria como
 * alteração e mandaria um PUT que não muda nada — e cada PUT grava uma linha de
 * auditoria dizendo que alguém mexeu nos filtros.
 */
export function serializeFilters(values: CollectionFilterValues): string {
  return JSON.stringify(
    Object.keys(values)
      .sort()
      .map((stream) => [
        stream,
        Object.keys(values[stream])
          .sort()
          .map((key) => [key, values[stream][key]]),
      ]),
  )
}

/**
 * Grava só o que filtra. Voltar ao default REMOVE a chave (e o stream, quando
 * fica vazio) em vez de gravar o default — assim o corpo do PUT e o estado do
 * banco não guardam lixo que parece configuração ativa.
 */
function withFilterValue(
  values: CollectionFilterValues,
  stream: string,
  field: CollectionFilterFieldRead,
  next: CollectionFilterValue | null,
): CollectionFilterValues {
  const out: CollectionFilterValues = { ...values }
  const streamValues = { ...(out[stream] ?? {}) }
  if (next === null || isFilterNoop(field, next)) {
    delete streamValues[field.key]
  } else {
    streamValues[field.key] = next
  }
  if (Object.keys(streamValues).length === 0) {
    delete out[stream]
  } else {
    out[stream] = streamValues
  }
  return out
}

interface PendingChange {
  stream: string
  field: CollectionFilterFieldRead
  next: CollectionFilterValue
}

interface CollectionFiltersSectionProps {
  /** Schema efetivo por stream, como o backend declara. Vazio ⇒ nada é renderizado. */
  availableFilters: Record<string, CollectionFilterFieldRead[]>
  values: CollectionFilterValues
  onChange: (next: CollectionFilterValues) => void
  disabled?: boolean
}

export const CollectionFiltersSection: React.FC<CollectionFiltersSectionProps> = ({
  availableFilters,
  values,
  onChange,
  disabled = false,
}) => {
  const { t } = useTranslation("integrations")
  // Rascunho do campo numérico: só é confirmado no blur/Enter. Sem isto, digitar
  // "12" pediria confirmação já no "1" — e o operador aprenderia a clicar em
  // "Ligar" sem ler, que é o oposto do que o diálogo existe para provocar.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})
  const [pending, setPending] = useState<PendingChange | null>(null)

  const streams = Object.entries(availableFilters).filter(([, fields]) => fields.length > 0)
  if (streams.length === 0) return null

  const activeCount = countActiveFilters(values)

  const cellId = (stream: string, key: string) => `${stream}::${key}`

  const apply = (
    stream: string,
    field: CollectionFilterFieldRead,
    next: CollectionFilterValue | null,
  ) => {
    onChange(withFilterValue(values, stream, field, next))
  }

  const clearDraft = (stream: string, key: string) =>
    setDrafts((prev) => {
      const out = { ...prev }
      delete out[cellId(stream, key)]
      return out
    })

  /**
   * Portão de consciência. Só a transição "sem filtro → filtrando" abre o
   * diálogo: voltar ao default é sempre seguro (nada passa a ser descartado), e
   * ajustar um filtro JÁ ligado não é uma decisão nova — o aviso do plugin
   * continua na tela o tempo todo enquanto o filtro estiver ativo.
   */
  const requestChange = (
    stream: string,
    field: CollectionFilterFieldRead,
    next: CollectionFilterValue,
  ): boolean => {
    const current = effectiveFilterValue(field, values, stream)
    if (isFilterNoop(field, next) || !isFilterNoop(field, current ?? undefined)) {
      apply(stream, field, next)
      return true
    }
    setPending({ stream, field, next })
    return false
  }

  const resetField = (stream: string, field: CollectionFilterFieldRead) => {
    clearDraft(stream, field.key)
    setFieldErrors((prev) => {
      const out = { ...prev }
      delete out[cellId(stream, field.key)]
      return out
    })
    apply(stream, field, field.default)
  }

  const resetAll = () => {
    setDrafts({})
    setFieldErrors({})
    onChange({})
  }

  const commitRange = (stream: string, field: CollectionFilterFieldRead) => {
    const id = cellId(stream, field.key)
    const raw = drafts[id]
    if (raw === undefined) return
    const min = field.min ?? Number.NEGATIVE_INFINITY
    const max = field.max ?? Number.POSITIVE_INFINITY
    const parsed = Number(raw.trim())
    // Campo esvaziado = voltar ao default. Qualquer outra entrada inválida é
    // recusada na tela: mandar ao backend um valor fora da faixa só voltaria 422.
    if (raw.trim() === "") {
      resetField(stream, field)
      return
    }
    if (!Number.isInteger(parsed) || parsed < min || parsed > max) {
      setFieldErrors((prev) => ({
        ...prev,
        [id]: t("form.collectionFilters.rangeInvalid", { min: field.min, max: field.max }),
      }))
      return
    }
    setFieldErrors((prev) => {
      const out = { ...prev }
      delete out[id]
      return out
    })
    // O rascunho só some quando a mudança de fato entrou. Enquanto o diálogo de
    // confirmação estiver aberto o campo continua mostrando o que o operador
    // digitou — o valor que o diálogo está perguntando sobre.
    if (requestChange(stream, field, parsed)) clearDraft(stream, field.key)
  }

  const renderControl = (stream: string, field: CollectionFilterFieldRead) => {
    const id = cellId(stream, field.key)
    const inputId = `collection-filter-${stream}-${field.key}`
    const current = effectiveFilterValue(field, values, stream)

    if (field.type === "bool") {
      return (
        <label className="flex items-center gap-2 text-sm text-text" htmlFor={inputId}>
          <input
            id={inputId}
            type="checkbox"
            className="h-4 w-4 rounded border-border"
            checked={Boolean(current)}
            onChange={(e) => requestChange(stream, field, e.target.checked)}
            disabled={disabled}
            data-testid={`collection-filter-input-${field.key}`}
          />
          <span>{t("form.collectionFilters.boolOn")}</span>
        </label>
      )
    }

    if (field.type === "enum") {
      return (
        <select
          id={inputId}
          className="h-9 w-full max-w-xs rounded-md border border-border bg-surface px-3 text-sm text-text focus-ring disabled:cursor-not-allowed disabled:opacity-50"
          value={String(current ?? "")}
          onChange={(e) => requestChange(stream, field, e.target.value)}
          disabled={disabled}
          data-testid={`collection-filter-input-${field.key}`}
        >
          {(field.options ?? []).map((opt) => (
            <option key={opt} value={opt}>
              {opt === field.default ? t("form.collectionFilters.enumNoFilterOption", { value: opt }) : opt}
            </option>
          ))}
        </select>
      )
    }

    // int_range — campo numérico com min/max, confirmado no blur/Enter.
    return (
      <input
        id={inputId}
        type="number"
        inputMode="numeric"
        min={field.min ?? undefined}
        max={field.max ?? undefined}
        step={1}
        className="h-9 w-32 rounded-md border border-border bg-surface px-3 text-sm text-text focus-ring disabled:cursor-not-allowed disabled:opacity-50"
        value={drafts[id] ?? String(current ?? "")}
        onChange={(e) => setDrafts((prev) => ({ ...prev, [id]: e.target.value }))}
        onBlur={() => commitRange(stream, field)}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault()
            commitRange(stream, field)
          }
        }}
        disabled={disabled}
        aria-invalid={fieldErrors[id] ? "true" : "false"}
        data-testid={`collection-filter-input-${field.key}`}
      />
    )
  }

  return (
    <section
      className="rounded-xl border border-border bg-surface-tertiary/40 p-4"
      aria-labelledby="collection-filters-heading"
      data-testid="collection-filters-section"
    >
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <FilterIcon size={16} className="text-text-secondary" aria-hidden="true" />
            <h3 id="collection-filters-heading" className="text-sm font-semibold text-text">
              {t("form.collectionFilters.title")}
            </h3>
            {activeCount > 0 ? (
              <Badge variant="warning" size="sm" data-testid="collection-filters-active-badge">
                {t("form.collectionFilters.activeSummary", { count: activeCount })}
              </Badge>
            ) : (
              <Badge variant="default" size="sm" data-testid="collection-filters-none-badge">
                {t("form.collectionFilters.noFilterSummary")}
              </Badge>
            )}
          </div>
          <p className="mt-1 text-xs text-text-secondary">{t("form.collectionFilters.subtitle")}</p>
        </div>
        {activeCount > 0 && (
          <Button
            type="button"
            variant="ghost"
            size="xs"
            leftIcon={<RotateCcwIcon size={12} />}
            onClick={resetAll}
            disabled={disabled}
            data-testid="collection-filters-reset-all"
          >
            {t("form.collectionFilters.resetAll")}
          </Button>
        )}
      </div>

      {/* Não-retroatividade fica visível sempre, não só no diálogo: é a parte que
          o operador esquece depois de configurar. */}
      <Notice variant="info" title={t("form.collectionFilters.notRetroactiveTitle")} className="mt-3">
        {t("form.collectionFilters.notRetroactive")}
      </Notice>

      <div className="mt-4 space-y-4">
        {streams.map(([stream, fields]) => (
          <fieldset key={stream} className="rounded-lg border border-border bg-surface p-3">
            <legend className="px-1 text-xs font-semibold uppercase tracking-wider text-text-tertiary">
              {t("form.collectionFilters.streamLabel", { stream })}
            </legend>
            <div className="space-y-4">
              {fields.map((field) => {
                const id = cellId(stream, field.key)
                const current = effectiveFilterValue(field, values, stream)
                const noop = isFilterNoop(field, current ?? undefined)
                return (
                  <div key={field.key} className="space-y-1.5" data-testid={`collection-filter-${field.key}`}>
                    <div className="flex flex-wrap items-center gap-2">
                      <label
                        className="text-sm font-medium text-text"
                        htmlFor={`collection-filter-${stream}-${field.key}`}
                      >
                        {field.label}
                      </label>
                      {noop ? (
                        <Badge variant="default" size="sm" data-testid={`collection-filter-state-${field.key}`}>
                          {t("form.collectionFilters.noFilter")}
                        </Badge>
                      ) : (
                        <>
                          <Badge variant="warning" size="sm" data-testid={`collection-filter-state-${field.key}`}>
                            {t("form.collectionFilters.active")}
                          </Badge>
                          <Button
                            type="button"
                            variant="ghost"
                            size="xs"
                            leftIcon={<RotateCcwIcon size={12} />}
                            onClick={() => resetField(stream, field)}
                            disabled={disabled}
                            data-testid={`collection-filter-reset-${field.key}`}
                          >
                            {t("form.collectionFilters.reset")}
                          </Button>
                        </>
                      )}
                    </div>

                    {renderControl(stream, field)}

                    {field.type === "int_range" && (
                      <p className="text-xs text-text-tertiary">
                        {/* `noFilterValue`, nunca `defaultValue`: esse nome é
                            reservado pelo i18next para o texto de fallback. Se a
                            chave sumisse de um locale, a frase inteira viraria o
                            próprio número — um "3" solto onde deveria estar
                            "Entre 0 e 15. Com 3 nada é filtrado.". */}
                        {t("form.collectionFilters.range", {
                          min: field.min,
                          max: field.max,
                          noFilterValue: String(field.default),
                        })}
                      </p>
                    )}
                    {fieldErrors[id] && (
                      <p className="text-xs text-danger-700" role="alert">
                        {fieldErrors[id]}
                      </p>
                    )}
                    {field.help_text && <p className="text-xs text-text-secondary">{field.help_text}</p>}

                    {/* O aviso do plugin fica na tela enquanto o filtro está
                        DESLIGADO (é o que o operador precisa ler antes de ligar)
                        e continua enquanto está ligado (é o que ele precisa
                        lembrar depois). */}
                    {field.warning_text && (
                      <div
                        className="flex items-start gap-2 rounded-md border border-warning-200 bg-warning-50 px-3 py-2 text-xs text-warning-800"
                        data-testid={`collection-filter-warning-${field.key}`}
                      >
                        <AlertTriangleIcon size={14} className="mt-0.5 shrink-0" aria-hidden="true" />
                        <span>
                          <strong className="block">{t("form.collectionFilters.warningLabel")}</strong>
                          {field.warning_text}
                        </span>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </fieldset>
        ))}
      </div>

      <ConfirmDialog
        open={pending !== null}
        title={t("form.collectionFilters.confirmTitle")}
        description={
          <div className="space-y-2">
            {pending?.field.warning_text && <p>{pending.field.warning_text}</p>}
            <p>{t("form.collectionFilters.notRetroactive")}</p>
            <p className="font-medium text-text">
              {t("form.collectionFilters.confirmNewValue", {
                label: pending?.field.label ?? "",
                value: String(pending?.next ?? ""),
              })}
            </p>
          </div>
        }
        confirmLabel={t("form.collectionFilters.confirmSubmit")}
        confirmVariant="danger"
        onConfirm={() => {
          if (pending) {
            apply(pending.stream, pending.field, pending.next)
            clearDraft(pending.stream, pending.field.key)
          }
          setPending(null)
        }}
        onClose={() => {
          // Cancelar não pode deixar o rascunho numérico na tela mostrando um
          // valor que NÃO está valendo.
          if (pending) clearDraft(pending.stream, pending.field.key)
          setPending(null)
        }}
        data-testid="collection-filters-confirm-dialog"
      />
    </section>
  )
}

export default CollectionFiltersSection
