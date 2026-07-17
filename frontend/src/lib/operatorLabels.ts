/**
 * operatorLabels — fonte ÚNICA dos rótulos de operador de condição de rota.
 *
 * PROBLEMA: na UI os operadores apareciam CRUS ("eq", "gte", "nin"…), crípticos
 * para o operador humano. Este helper mapeia cada operador → um rótulo amigável
 * (formato híbrido: símbolo matemático + palavra curta traduzida, ex. "≥ maior
 * ou igual"), lido de um único bloco i18n.
 *
 * CONTRATO INTACTO: só o LABEL exibido muda. O `value` enviado ao backend
 * continua sendo o operador CRU (eq/ne/gt/gte/lt/lte/in/nin/exists), espelhando
 * routing.engine.ALLOWED_OPS. Nenhum componente deve traduzir o value.
 *
 * FONTE ÚNICA: as strings vivem em `common.conditionOperators.*` (namespace
 * default, sempre carregado) — assim qualquer superfície, atual ou futura
 * (inclusive telas EE fora deste repo), reusa a mesma fonte sem se acoplar ao
 * namespace de uma tela específica.
 */
import { useTranslation } from "react-i18next"

/** Operadores de condição de rota — espelha routing.engine.ALLOWED_OPS no backend. */
export const CONDITION_OPERATORS = [
  "eq",
  "ne",
  "gt",
  "gte",
  "lt",
  "lte",
  "in",
  "nin",
  "exists",
] as const

export type ConditionOperator = (typeof CONDITION_OPERATORS)[number]

export interface OperatorOption {
  /** Value CRU enviado ao backend — NUNCA traduzido. */
  value: string
  /** Rótulo amigável exibido ao usuário. */
  label: string
}

/**
 * Hook que expõe os rótulos amigáveis dos operadores de condição.
 *
 * - `label(op)` → rótulo amigável do operador (fallback: o próprio op cru, para
 *   um operador desconhecido nunca virar chave i18n crua na tela).
 * - `options(ops?)` → array `{ value, label }` pronto para o `<Select>`; o value
 *   permanece o operador cru. Sem argumento, usa a lista canônica completa.
 */
export function useConditionOperatorLabels() {
  const { t } = useTranslation("common")
  const label = (op: string): string =>
    t(`conditionOperators.${op}`, { defaultValue: op })
  const options = (ops: readonly string[] = CONDITION_OPERATORS): OperatorOption[] =>
    ops.map((op) => ({ value: op, label: label(op) }))
  return { label, options }
}
