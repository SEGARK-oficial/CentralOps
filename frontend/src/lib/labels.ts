/**
 * Rótulos PT-BR para enums vindos do backend.
 *
 * O VALOR enviado/recebido do backend permanece em inglês; estas funções só
 * traduzem o que é EXIBIDO ao usuário. Sempre caem de volta no valor cru quando
 * o enum é desconhecido, para nunca esconder informação.
 */

type BadgeVariant = "success" | "warning" | "danger" | "primary" | "outline" | "default"

// ── Auth / health status de integração ──────────────────────────────
const AUTH_STATUS_LABEL: Record<string, string> = {
  healthy: "Saudável",
  degraded: "Degradado",
  error: "Erro",
  unknown: "Desconhecido",
}

const AUTH_STATUS_VARIANT: Record<string, BadgeVariant> = {
  healthy: "success",
  degraded: "warning",
  error: "danger",
  unknown: "outline",
}

export function authStatusLabel(status?: string | null): string {
  if (!status) return AUTH_STATUS_LABEL.unknown
  return AUTH_STATUS_LABEL[status.toLowerCase()] ?? status
}

export function authStatusVariant(status?: string | null): BadgeVariant {
  if (!status) return "outline"
  return AUTH_STATUS_VARIANT[status.toLowerCase()] ?? "default"
}

// ── Severidade de alerta ─────────────────────────────────────────────
const SEVERITY_LABEL: Record<string, string> = {
  critical: "Crítico",
  high: "Alto",
  medium: "Médio",
  low: "Baixo",
  info: "Informativo",
  informational: "Informativo",
}

const SEVERITY_VARIANT: Record<string, BadgeVariant> = {
  critical: "danger",
  high: "warning",
  medium: "primary",
  low: "default",
  info: "outline",
  informational: "outline",
}

export function severityLabel(severity?: string | null): string {
  if (!severity) return "—"
  return SEVERITY_LABEL[severity.toLowerCase()] ?? severity
}

export function severityVariant(severity?: string | null): BadgeVariant {
  if (!severity) return "default"
  return SEVERITY_VARIANT[severity.toLowerCase()] ?? "default"
}

// ── Status de ativo (inventário) ─────────────────────────────────────
const ASSET_STATUS_LABEL: Record<string, string> = {
  active: "Ativo",
  inactive: "Inativo",
  online: "Online",
  offline: "Offline",
  isolated: "Isolado",
  pending: "Pendente",
  unknown: "Desconhecido",
}

export function assetStatusLabel(status?: string | null): string {
  if (!status) return "—"
  return ASSET_STATUS_LABEL[status.toLowerCase()] ?? status
}
