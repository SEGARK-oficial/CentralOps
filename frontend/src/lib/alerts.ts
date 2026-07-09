import type { Alert, AlertFilters } from "@/types"
import { ApiRequestError } from "@/services/api"

export const DEFAULT_ALERT_INDEX = "wazuh-alerts-*"

export function normalizeAlertIndex(index?: string | null): string {
  const normalized = index?.trim()
  if (!normalized) return DEFAULT_ALERT_INDEX
  if (["none", "null", "undefined"].includes(normalized.toLowerCase())) {
    return DEFAULT_ALERT_INDEX
  }
  return normalized
}

export function withDefaultAlertIndex(filters?: AlertFilters): AlertFilters {
  return {
    ...filters,
    index: normalizeAlertIndex(filters?.index),
  }
}

export function getAlertDetailFilters(
  alert?: Pick<Alert, "source_index"> | null,
  filters?: AlertFilters,
): Pick<AlertFilters, "index"> {
  return {
    index: normalizeAlertIndex(alert?.source_index || filters?.index),
  }
}

export function getAlertRequestErrorMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiRequestError) {
    switch (error.code) {
      case "ALERT_INDEX_INVALID":
        return "O indice de alertas informado nao existe ou nao esta configurado no indexer."
      case "INDEXER_NOT_CONFIGURED":
      case "INDEXER_CREDENTIALS_MISSING":
      case "INDEXER_AUTH_FAILED":
        return "A integracao Wazuh nao tem o indexer configurado corretamente para consultar alertas."
      case "INDEXER_UNAVAILABLE":
        return "O indexer do Wazuh esta indisponivel no momento."
      case "ALERT_NOT_FOUND":
        return "O alerta solicitado nao foi encontrado."
      default:
        return error.message || fallback
    }
  }

  return error instanceof Error ? error.message : fallback
}
