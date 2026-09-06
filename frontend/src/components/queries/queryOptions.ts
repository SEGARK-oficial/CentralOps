import type { QueryFindingShape } from "@/types"

/**
 * Severidade OCSF que a query grava na Detection e carrega nos eventos
 * 1006/2004. O valor vira o PRI do syslog e o level da regra no Wazuh: uma
 * hunt exploratória em Critical gera alerta level 12 a cada execução.
 */
export const QUERY_SEVERITY_OPTIONS: { value: number; label: string }[] = [
  { value: 1, label: "Informativa (1)" },
  { value: 2, label: "Baixa (2)" },
  { value: 3, label: "Média (3)" },
  { value: 4, label: "Alta (4) — padrão" },
  { value: 5, label: "Crítica (5)" },
]

export const DEFAULT_QUERY_SEVERITY = 4

export const QUERY_FINDING_SHAPE_OPTIONS: { value: QueryFindingShape; label: string }[] = [
  { value: "both", label: "Resumo + um achado por linha — padrão" },
  { value: "per_row", label: "Um achado por linha" },
  { value: "summary", label: "Só o resumo (tabela em evidences[])" },
]

export const DEFAULT_QUERY_FINDING_SHAPE: QueryFindingShape = "both"

export const QUERY_FINDING_SHAPE_HELP =
  "Um achado por linha vira campos planos (host, usuário, processo) no Wazuh e um asset no IRIS; o resumo carrega a tabela inteira num evento só."
