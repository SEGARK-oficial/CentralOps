import type { TFunction } from "i18next"
import type { QueryFindingShape } from "@/types"

/**
 * Severidade OCSF que a query grava na Detection e carrega nos eventos
 * 1006/2004. O valor vira o PRI do syslog e o level da regra no Wazuh: uma
 * hunt exploratória em Critical gera alerta level 12 a cada execução.
 *
 * A11Y-23: os rótulos são texto voltado ao usuário — viraram função de `t`
 * (as constantes originais eram module-level, sem acesso a hook de i18n).
 */
export function getQuerySeverityOptions(t: TFunction): { value: number; label: string }[] {
  return [
    { value: 1, label: t("queries:severityOptions.informational") },
    { value: 2, label: t("queries:severityOptions.low") },
    { value: 3, label: t("queries:severityOptions.medium") },
    { value: 4, label: t("queries:severityOptions.high") },
    { value: 5, label: t("queries:severityOptions.critical") },
  ]
}

export const DEFAULT_QUERY_SEVERITY = 4

export function getQueryFindingShapeOptions(t: TFunction): { value: QueryFindingShape; label: string }[] {
  return [
    { value: "both", label: t("queries:findingShapeOptions.both") },
    { value: "per_row", label: t("queries:findingShapeOptions.perRow") },
    { value: "summary", label: t("queries:findingShapeOptions.summary") },
  ]
}

export const DEFAULT_QUERY_FINDING_SHAPE: QueryFindingShape = "both"

export function getQueryFindingShapeHelp(t: TFunction): string {
  return t("queries:findingShapeOptions.help")
}
