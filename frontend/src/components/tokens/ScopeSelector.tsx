"use client"

import type React from "react"
import { useEffect, useMemo, useState } from "react"
import { LockIcon, ShieldCheckIcon } from "lucide-react"

import * as api from "@/services/api"
import type { ScopeName } from "@/types"
import { LoadingSpinner } from "@/components/ui/LoadingSpinner/LoadingSpinner"
import { Notice } from "@/components/ui/Notice/Notice"

interface ScopeSelectorProps {
  /** Currently selected scopes. ``null`` means "full inherit" (no checkboxes
   *  selected, hint shown). Empty array same effect, but explicit. */
  value: ScopeName[] | null
  onChange: (scopes: ScopeName[] | null) => void
  /** When true, disables interaction (e.g. while submitting). */
  disabled?: boolean
  /** Hides the "full inherit" toggle — useful when caller already enforces
   *  least-privilege (e.g. Service Accounts em produção). */
  requireExplicit?: boolean
}

// Agrupamento estável (não vem do backend — categoria é detalhe de UI).
// Scopes desconhecidos vão pra "Outros" automaticamente.
const SCOPE_CATEGORIES: Record<string, string> = {
  "mapping.read": "Mappings",
  "mapping.write": "Mappings",
  "mapping.rollback": "Mappings",
  "integration.read": "Integrações",
  "integration.write": "Integrações",
  "integration.pause": "Integrações",
  "quarantine.read": "Quarantine & Drift",
  "quarantine.discard": "Quarantine & Drift",
  "drift.read": "Quarantine & Drift",
  "drift.ignore": "Quarantine & Drift",
  "drift.mark_mapped": "Quarantine & Drift",
  "drift.delete": "Quarantine & Drift",
  "user.manage": "Administração",
  "secret.read": "Administração",
  "audit.read": "Administração",
  "org.manage": "Administração",
  "internal.tenant.read": "Internal API",
}

const CATEGORY_ORDER = [
  "Integrações",
  "Mappings",
  "Quarantine & Drift",
  "Administração",
  "Internal API",
  "Outros",
]

/** Brief description shown next to each scope. Hardcoded — não vem do
 *  backend pra evitar i18n round-trip. Mantido em sync com docs/api-tokens.md. */
const SCOPE_DESCRIPTIONS: Partial<Record<ScopeName, string>> = {
  "mapping.read": "Listar mappings, ver versões e diffs",
  "mapping.write": "Editar mappings (commit de versão)",
  "mapping.rollback": "Reverter mapping pra versão anterior",
  "integration.read": "Ler integrações, health, capabilities",
  "integration.write": "Criar/editar/deletar integrações",
  "integration.pause": "Pausar/retomar integração ativa",
  "quarantine.read": "Listar/inspecionar eventos quarantine",
  "quarantine.discard": "Descartar/reprocessar quarantine",
  "drift.read": "Ler campos drift descobertos",
  "drift.ignore": "Marcar drift como ignorado",
  "drift.mark_mapped": "Marcar drift como mapeado",
  "drift.delete": "Deletar entradas de drift",
  "user.manage": "CRUD de usuários e Service Accounts",
  "secret.read": "Ver client_secrets cifrados",
  "audit.read": "Listar audit log",
  "org.manage": "CRUD de organizations",
  "internal.tenant.read": "Consumir /api/internal/tenants/* (IASOC)",
}

export const ScopeSelector: React.FC<ScopeSelectorProps> = ({
  value,
  onChange,
  disabled = false,
  requireExplicit = false,
}) => {
  const [available, setAvailable] = useState<ScopeName[] | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api
      .listScopes()
      .then((scopes) => {
        if (cancelled) return
        setAvailable(scopes)
        setError(null)
      })
      .catch((e) => {
        if (cancelled) return
        setError(e instanceof Error ? e.message : "Falha ao carregar scopes")
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })
    return () => {
      cancelled = true
    }
  }, [])

  // Agrupa scopes carregados pelas categorias.
  const grouped = useMemo(() => {
    if (!available) return new Map<string, ScopeName[]>()
    const m = new Map<string, ScopeName[]>()
    for (const cat of CATEGORY_ORDER) m.set(cat, [])
    for (const scope of available) {
      const cat = SCOPE_CATEGORIES[scope] ?? "Outros"
      const list = m.get(cat) ?? []
      list.push(scope)
      m.set(cat, list)
    }
    return m
  }, [available])

  const fullInherit = value === null || value.length === 0

  const toggleScope = (scope: ScopeName) => {
    if (disabled) return
    const current = value ?? []
    const next = current.includes(scope)
      ? current.filter((s) => s !== scope)
      : [...current, scope]
    // Empty list when allowed → null (semantically same, more explicit).
    if (next.length === 0 && !requireExplicit) {
      onChange(null)
    } else {
      onChange(next)
    }
  }

  const setFullInherit = () => {
    if (disabled || requireExplicit) return
    onChange(null)
  }

  if (loading) {
    return (
      <div className="rounded-md border border-border bg-bg-subtle p-4">
        <LoadingSpinner size="sm" text="Carregando lista de scopes…" />
      </div>
    )
  }

  if (error || !available) {
    return (
      <Notice variant="danger">
        Falha ao listar scopes: {error ?? "resposta vazia"}. Token será criado
        com herança completa de permissões.
      </Notice>
    )
  }

  return (
    <div className="space-y-3">
      {!requireExplicit && (
        <label className="flex items-start gap-2 rounded-md border border-border p-3 cursor-pointer hover:bg-bg-subtle">
          <input
            type="radio"
            checked={fullInherit}
            onChange={setFullInherit}
            disabled={disabled}
            className="mt-1"
          />
          <div className="flex-1">
            <div className="flex items-center gap-2 text-sm font-semibold">
              <ShieldCheckIcon size={16} />
              Herdar permissões da conta (padrão)
            </div>
            <p className="mt-1 text-xs text-text-secondary">
              Token tem o mesmo nível de acesso da sua conta. Equivale a Fase 1
              (sem scopes). Se você for despromovido, o token perde acesso
              automaticamente.
            </p>
          </div>
        </label>
      )}

      <label className="flex items-start gap-2 rounded-md border border-border p-3 cursor-pointer hover:bg-bg-subtle">
        <input
          type="radio"
          checked={!fullInherit || requireExplicit}
          onChange={() => !disabled && onChange(value ?? [])}
          disabled={disabled}
          className="mt-1"
        />
        <div className="flex-1">
          <div className="flex items-center gap-2 text-sm font-semibold">
            <LockIcon size={16} />
            Restringir a scopes específicos (least privilege)
          </div>
          <p className="mt-1 text-xs text-text-secondary">
            Marque apenas o que o cliente precisa. Permissão efetiva =
            interseção do role do dono com os scopes selecionados.
          </p>
        </div>
      </label>

      {(!fullInherit || requireExplicit) && (
        <div className="space-y-3 rounded-md border border-border-strong bg-bg p-3">
          {CATEGORY_ORDER.map((cat) => {
            const scopes = grouped.get(cat)
            if (!scopes || scopes.length === 0) return null
            return (
              <div key={cat}>
                <div className="text-xs font-semibold uppercase tracking-wider text-text-secondary">
                  {cat}
                </div>
                <div className="mt-1 grid gap-1">
                  {scopes.map((scope) => (
                    <label
                      key={scope}
                      className="flex items-start gap-2 rounded p-1.5 hover:bg-bg-subtle cursor-pointer"
                    >
                      <input
                        type="checkbox"
                        checked={value?.includes(scope) ?? false}
                        onChange={() => toggleScope(scope)}
                        disabled={disabled}
                        className="mt-0.5"
                      />
                      <div className="flex-1 text-sm">
                        <code className="font-mono text-xs">{scope}</code>
                        {SCOPE_DESCRIPTIONS[scope] && (
                          <span className="ml-2 text-xs text-text-secondary">
                            {SCOPE_DESCRIPTIONS[scope]}
                          </span>
                        )}
                      </div>
                    </label>
                  ))}
                </div>
              </div>
            )
          })}
          {value && value.length > 0 && (
            <div className="border-t pt-2 text-xs text-text-secondary">
              <strong>{value.length}</strong>{" "}
              {value.length === 1 ? "scope" : "scopes"} selecionado(s)
            </div>
          )}
        </div>
      )}
    </div>
  )
}
