"use client"

import type React from "react"
import { useEffect, useMemo, useRef, useState } from "react"
import { createPortal } from "react-dom"
import {
  CopyIcon,
  FilterIcon,
  NetworkIcon,
  ShieldAlertIcon,
  TerminalSquareIcon,
  UserIcon,
  XIcon,
} from "lucide-react"
import type { Alert } from "@/types"
import { Badge } from "@/components/ui/Badge/Badge"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Notice } from "@/components/ui/Notice/Notice"
import { severityLabel, severityVariant } from "@/lib/labels"
import { copyToClipboard, formatDate } from "@/lib/utils"

interface AlertDetailsDrawerProps {
  open: boolean
  alert: Alert | null
  loading?: boolean
  error?: string | null
  onClose: () => void
  onPivotRuleId?: (value: string) => void
  onPivotHostname?: (value: string) => void
}

function renderHighlightFragment(fragment: string, key: string) {
  const tokens = fragment.split(/(<em>|<\/em>)/g)
  let highlighted = false

  return (
    <span key={key}>
      {tokens.map((token, index) => {
        if (token === "<em>") {
          highlighted = true
          return null
        }
        if (token === "</em>") {
          highlighted = false
          return null
        }
        if (!token) return null
        return highlighted ? (
          <mark key={`${key}-${index}`} className="rounded bg-warning-200 px-0.5 text-text">
            {token}
          </mark>
        ) : (
          <span key={`${key}-${index}`}>{token}</span>
        )
      })}
    </span>
  )
}

function buildInvestigationQuery(alert: Alert): string {
  const parts: string[] = []
  if (alert.hostname) parts.push(`agent.name:"${alert.hostname}"`)
  if (alert.rule_id) parts.push(`rule.id:${alert.rule_id}`)
  if (alert.src_ip) parts.push(`data.srcip:${alert.src_ip}`)
  return parts.join(" AND ") || `"_id":"${alert.alert_id}"`
}

const sectionTitleCls = "text-sm font-semibold uppercase tracking-wider text-text-secondary"
const valueCls = "break-words text-sm text-text"

export const AlertDetailsDrawer: React.FC<AlertDetailsDrawerProps> = ({
  open,
  alert,
  loading = false,
  error,
  onClose,
  onPivotRuleId,
  onPivotHostname,
}) => {
  const drawerRef = useRef<HTMLDivElement>(null)
  const previousActiveElement = useRef<HTMLElement | null>(null)
  const [copyFeedback, setCopyFeedback] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return

    previousActiveElement.current = document.activeElement as HTMLElement
    // Foco inicial no painel; o ref só está disponível após o commit do portal.
    const focusTimer = window.setTimeout(() => drawerRef.current?.focus(), 0)
    document.body.style.overflow = "hidden"

    const FOCUSABLE_SELECTOR =
      'a[href], button:not([disabled]), textarea:not([disabled]), input:not([disabled]), select:not([disabled]), summary, details, [tabindex]:not([tabindex="-1"])'

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault()
        onClose()
        return
      }

      if (event.key !== "Tab") return

      const panel = drawerRef.current
      if (!panel) return

      const focusable = Array.from(panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter(
        (element) => element.offsetParent !== null || element === document.activeElement,
      )
      if (focusable.length === 0) {
        // Sem itens focáveis: mantém o foco preso no próprio painel.
        event.preventDefault()
        panel.focus()
        return
      }

      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      const active = document.activeElement

      if (event.shiftKey) {
        if (active === first || active === panel || !panel.contains(active)) {
          event.preventDefault()
          last.focus()
        }
      } else if (active === last || !panel.contains(active)) {
        event.preventDefault()
        first.focus()
      }
    }

    document.addEventListener("keydown", handleKeyDown)
    return () => {
      window.clearTimeout(focusTimer)
      document.removeEventListener("keydown", handleKeyDown)
      document.body.style.overflow = ""
      previousActiveElement.current?.focus()
    }
  }, [onClose, open])

  useEffect(() => {
    if (!copyFeedback) return
    const timeout = window.setTimeout(() => setCopyFeedback(null), 1800)
    return () => window.clearTimeout(timeout)
  }, [copyFeedback])

  const queryToCopy = useMemo(() => (alert ? buildInvestigationQuery(alert) : ""), [alert])

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 z-modal-backdrop bg-black/45" onClick={onClose}>
      <div
        ref={drawerRef}
        className="ml-auto h-full w-full sm:max-w-2xl lg:max-w-3xl"
        onClick={(event) => event.stopPropagation()}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-label="Detalhes do alerta"
      >
      <div className="flex h-full w-full flex-col overflow-hidden border-l border-border bg-surface shadow-2xl">
        <div className="shrink-0 border-b border-border px-6 py-5">
          <div className="flex items-start justify-between gap-4">
            <div className="min-w-0 space-y-2">
            <div className="flex flex-wrap items-center gap-2">
              <Badge variant={severityVariant(alert?.severity)} size="sm">
                {alert?.severity ? severityLabel(alert.severity) : "alerta"}
              </Badge>
              {alert?.rule_id && (
                <Badge variant="outline" size="sm">
                  Regra {alert.rule_id}
                </Badge>
              )}
              {alert?.integration_name && (
                <Badge variant="outline" size="sm">
                  {alert.integration_name}
                </Badge>
              )}
            </div>
            <div className="min-w-0">
              <h2 className="break-words text-xl font-semibold text-text">{alert?.title || "Detalhes do alerta"}</h2>
              <p className="mt-1 break-all text-sm text-text-secondary">
                {alert?.timestamp ? formatDate(alert.timestamp) : "Sem timestamp"}
                {alert?.hostname ? ` · ${alert.hostname}` : ""}
              </p>
            </div>
          </div>

          <Button variant="ghost" size="xs" onClick={onClose} aria-label="Fechar detalhes">
            <XIcon size={18} />
          </Button>
        </div>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-y-auto px-6 py-5">
          {loading && <div className="py-8 text-center text-sm text-text-secondary">Carregando detalhes do alerta...</div>}

          {error && (
            <Notice variant="danger" title="Falha ao carregar detalhes">
              {error}
            </Notice>
          )}

          {!loading && alert && (
            <>
              {copyFeedback && (
                <Notice variant="success" title="Ação rápida concluída">
                  {copyFeedback}
                </Notice>
              )}

              <Card padding="md" className="space-y-4 shadow-sm">
                <div className="flex items-center gap-2">
                  <ShieldAlertIcon size={16} className="text-text-tertiary" />
                  <h3 className={sectionTitleCls}>Resumo</h3>
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Alert ID</div>
                    <div className="flex items-center gap-2">
                      <div className={`${valueCls} break-all font-mono text-xs`}>{alert.alert_id || "-"}</div>
                      <Button
                        variant="ghost"
                        size="xs"
                        onClick={async () => {
                          const ok = await copyToClipboard(alert.alert_id)
                          setCopyFeedback(ok ? "Alert ID copiado para a área de transferência." : "Não foi possível copiar o Alert ID.")
                        }}
                      >
                        Copiar
                      </Button>
                    </div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Data e hora</div>
                    <div className={valueCls}>{alert.timestamp ? formatDate(alert.timestamp) : "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Host</div>
                    <div className={valueCls}>{alert.hostname || alert.agent_name || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">ID da regra</div>
                    <div className={`${valueCls} break-all font-mono text-xs`}>{alert.rule_id || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Nível</div>
                    <div className={valueCls}>{alert.rule_level ?? "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Manager</div>
                    <div className={valueCls}>{alert.manager_name || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Índice de origem</div>
                    <div className={`${valueCls} break-all font-mono text-xs`}>{alert.source_index || "-"}</div>
                  </div>
                </div>
              </Card>

              <Card padding="md" className="space-y-4 shadow-sm">
                <div className="flex items-center gap-2">
                  <UserIcon size={16} className="text-text-tertiary" />
                  <h3 className={sectionTitleCls}>Contexto do agente</h3>
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <div className="text-xs font-medium text-text-secondary">ID do agente</div>
                    <div className={`${valueCls} break-all font-mono text-xs`}>{alert.agent_id || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Nome do agente</div>
                    <div className={valueCls}>{alert.agent_name || alert.hostname || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">IP</div>
                    <div className={`${valueCls} font-mono text-xs`}>{alert.agent_ip || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Grupo</div>
                    <div className={valueCls}>{alert.agent_group || "-"}</div>
                  </div>
                </div>
                {Object.keys(alert.agent_labels ?? {}).length > 0 && (
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Labels</div>
                    <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded-xl bg-surface-tertiary p-3 text-xs text-text-secondary">
                      {JSON.stringify(alert.agent_labels, null, 2)}
                    </pre>
                  </div>
                )}
              </Card>

              <Card padding="md" className="space-y-4 shadow-sm">
                <div className="flex items-center gap-2">
                  <FilterIcon size={16} className="text-text-tertiary" />
                  <h3 className={sectionTitleCls}>Regra e detecção</h3>
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Grupos</div>
                    <div className={valueCls}>{alert.rule_groups.length > 0 ? alert.rule_groups.join(", ") : "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Disparos</div>
                    <div className={valueCls}>{alert.rule_firedtimes ?? "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Decodificador</div>
                    <div className={valueCls}>{alert.decoder_name || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Localização</div>
                    <div className={valueCls}>{alert.location || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Tipo de entrada</div>
                    <div className={valueCls}>{alert.input_type || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Caminho do syscheck</div>
                    <div className={`${valueCls} break-all`}>{alert.syscheck_path || "-"}</div>
                  </div>
                </div>
              </Card>

              <Card padding="md" className="space-y-4 shadow-sm">
                <div className="flex items-center gap-2">
                  <NetworkIcon size={16} className="text-text-tertiary" />
                  <h3 className={sectionTitleCls}>Evidência</h3>
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <div className="text-xs font-medium text-text-secondary">IP de origem</div>
                    <div className={`${valueCls} font-mono text-xs`}>{alert.src_ip || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">IP de destino</div>
                    <div className={`${valueCls} font-mono text-xs`}>{alert.dst_ip || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Usuário de origem</div>
                    <div className={valueCls}>{alert.src_user || "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Usuário de destino</div>
                    <div className={valueCls}>{alert.dst_user || "-"}</div>
                  </div>
                </div>

                {Object.keys(alert.highlights ?? {}).length > 0 && (
                  <div className="space-y-2">
                    <div className="text-xs font-medium text-text-secondary">Trechos casados</div>
                    {Object.entries(alert.highlights).map(([field, fragments]) => (
                      <div key={field} className="rounded-xl border border-border bg-surface-tertiary/50 p-3 text-sm text-text">
                        <div className="mb-2 text-xs font-semibold uppercase tracking-wider text-text-tertiary">{field}</div>
                        <div className="space-y-2">
                          {fragments.map((fragment, index) => (
                            <div key={`${field}-${index}`}>{renderHighlightFragment(fragment, `${field}-${index}`)}</div>
                          ))}
                        </div>
                      </div>
                    ))}
                  </div>
                )}

                {alert.full_log && (
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Log completo</div>
                    <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded-xl bg-surface-tertiary p-3 text-xs text-text-secondary">
                      {alert.full_log}
                    </pre>
                  </div>
                )}

                {Object.keys(alert.data_fields ?? {}).length > 0 && (
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Campos dinâmicos</div>
                    <pre className="mt-2 overflow-x-auto whitespace-pre-wrap break-all rounded-xl bg-surface-tertiary p-3 text-xs text-text-secondary">
                      {JSON.stringify(alert.data_fields, null, 2)}
                    </pre>
                  </div>
                )}
              </Card>

              <Card padding="md" className="space-y-4 shadow-sm">
                <div className="flex items-center gap-2">
                  <TerminalSquareIcon size={16} className="text-text-tertiary" />
                  <h3 className={sectionTitleCls}>MITRE e ações rápidas</h3>
                </div>
                <div className="grid gap-4 sm:grid-cols-2">
                  <div>
                    <div className="text-xs font-medium text-text-secondary">MITRE IDs</div>
                    <div className={valueCls}>{alert.mitre_ids.length > 0 ? alert.mitre_ids.join(", ") : "-"}</div>
                  </div>
                  <div>
                    <div className="text-xs font-medium text-text-secondary">Táticas</div>
                    <div className={valueCls}>{alert.mitre_tactics.length > 0 ? alert.mitre_tactics.join(", ") : "-"}</div>
                  </div>
                </div>

                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    leftIcon={<CopyIcon size={14} />}
                    onClick={async () => {
                      const ok = await copyToClipboard(queryToCopy)
                      setCopyFeedback(ok ? "Consulta copiada para a área de transferência." : "Não foi possível copiar a consulta.")
                    }}
                  >
                    Copiar consulta de investigação
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    leftIcon={<FilterIcon size={14} />}
                    onClick={() => alert?.rule_id && onPivotRuleId?.(alert.rule_id)}
                    disabled={!alert?.rule_id}
                  >
                    Filtrar pela mesma rule
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    leftIcon={<FilterIcon size={14} />}
                    onClick={() => alert?.hostname && onPivotHostname?.(alert.hostname)}
                    disabled={!alert?.hostname}
                  >
                    Filtrar pelo mesmo host
                  </Button>
                </div>
              </Card>

              <details className="rounded-2xl border border-border bg-surface shadow-sm">
                <summary className="cursor-pointer list-none px-5 py-4 text-sm font-semibold text-text">
                  JSON bruto
                </summary>
                <div className="border-t border-border px-5 py-4">
                  <pre className="overflow-x-auto whitespace-pre-wrap break-all rounded-xl bg-surface-tertiary p-3 text-xs text-text-secondary">
                    {JSON.stringify(alert.raw, null, 2)}
                  </pre>
                </div>
              </details>
            </>
          )}
        </div>
      </div>
      </div>
    </div>,
    document.body,
  )
}

export default AlertDetailsDrawer
