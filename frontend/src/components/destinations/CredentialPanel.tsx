/**
 * CredentialPanel — ações de rotação/revogação de credencial + auditoria de acesso.
 *
 * S5: rotateCredential (form com novo segredo + expires_at opcional)
 *     revokeCredential (ConfirmDialog — avisa que desabilita o destino)
 * S6: getCredentialAudit — lista entradas de acesso (actor/action/created_at)
 *
 * Segredo NUNCA é exibido — apenas informado durante rotação (WRITE-ONLY).
 */

import type React from "react"
import { useCallback, useState } from "react"
import { KeyRoundIcon, ShieldOffIcon, RefreshCcwIcon, ClockIcon } from "lucide-react"
import * as api from "@/services/api"
import { Button } from "@/components/ui/Button/Button"
import { Card } from "@/components/ui/Card/Card"
import { Badge } from "@/components/ui/Badge/Badge"
import { Notice } from "@/components/ui/Notice/Notice"
import { ConfirmDialog } from "@/components/ui/ConfirmDialog/ConfirmDialog"
import { Modal } from "@/components/ui/Modal/Modal"
import { SkeletonText } from "@/components/ui/Skeleton"
import { ErrorState } from "@/components/ui/ErrorState"
import { useAsyncResource } from "@/hooks/useAsyncResource"
import type {
  CredentialAuditResponse,
  CredentialAccessEntry,
  CredentialRotateRequest,
} from "@/types"

// ── Sub-componente: formulário de rotação ─────────────────────────────────────

interface RotateFormProps {
  loading: boolean
  onSubmit: (req: CredentialRotateRequest) => Promise<void>
  onCancel: () => void
}

const RotateForm: React.FC<RotateFormProps> = ({ loading, onSubmit, onCancel }) => {
  const [newSecret, setNewSecret] = useState("")
  const [expiresAt, setExpiresAt] = useState("")

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault()
    void onSubmit({
      new_secret: newSecret,
      expires_at: expiresAt || null,
    })
  }

  return (
    <form onSubmit={handleSubmit} className="space-y-4">
      <div className="flex flex-col gap-1.5">
        <label
          htmlFor="rotate-secret"
          className="text-sm font-medium text-text"
        >
          Novo segredo <span className="text-danger-500" aria-label="obrigatório">*</span>
        </label>
        <input
          id="rotate-secret"
          type="password"
          required
          autoComplete="new-password"
          value={newSecret}
          onChange={(e) => setNewSecret(e.target.value)}
          placeholder="Cole o novo segredo aqui"
          className="w-full h-9 px-3 text-sm rounded-md border border-border bg-surface text-text placeholder:text-text-tertiary transition-colors focus-ring"
          data-testid="rotate-secret-input"
        />
        <p className="text-xs text-text-tertiary">
          O valor é cifrado no backend — nunca é exibido após envio.
        </p>
      </div>

      <div className="flex flex-col gap-1.5">
        <label
          htmlFor="rotate-expires"
          className="text-sm font-medium text-text"
        >
          Validade (opcional)
        </label>
        <input
          id="rotate-expires"
          type="datetime-local"
          value={expiresAt}
          onChange={(e) => setExpiresAt(e.target.value)}
          className="w-full h-9 px-3 text-sm rounded-md border border-border bg-surface text-text transition-colors focus-ring"
          data-testid="rotate-expires-input"
        />
        <p className="text-xs text-text-tertiary">
          Deixe em branco para credencial sem expiração.
        </p>
      </div>

      <div className="flex justify-end gap-3 pt-2">
        <Button type="button" variant="outline" onClick={onCancel} disabled={loading}>
          Cancelar
        </Button>
        <Button
          type="submit"
          variant="primary"
          loading={loading}
          disabled={!newSecret.trim()}
          data-testid="rotate-submit-btn"
        >
          Rotacionar credencial
        </Button>
      </div>
    </form>
  )
}

// ── Sub-componente: linha de auditoria ────────────────────────────────────────

const ACTION_LABEL: Record<string, string> = {
  decrypt: "Leitura",
  test: "Teste",
  rotate: "Rotação",
  revoke: "Revogação",
}

const ACTION_VARIANT: Record<string, "default" | "warning" | "danger" | "success" | "outline"> = {
  decrypt: "outline",
  test: "default",
  rotate: "warning",
  revoke: "danger",
}

const AuditRow: React.FC<{ entry: CredentialAccessEntry }> = ({ entry }) => (
  <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border py-2 last:border-b-0">
    <div className="flex flex-wrap items-center gap-2">
      <Badge variant={ACTION_VARIANT[entry.action] ?? "outline"} size="sm">
        {ACTION_LABEL[entry.action] ?? entry.action}
      </Badge>
      <span className="text-sm text-text">{entry.actor ?? "(sistema)"}</span>
      {entry.detail && (
        <span className="text-xs text-text-tertiary">— {entry.detail}</span>
      )}
    </div>
    <div className="flex items-center gap-1 text-xs text-text-tertiary">
      <ClockIcon size={12} aria-hidden="true" />
      <time dateTime={entry.created_at}>
        {new Date(entry.created_at).toLocaleString("pt-BR")}
      </time>
    </div>
  </div>
)

// ── Componente principal ──────────────────────────────────────────────────────

export interface CredentialPanelProps {
  destinationId: string
  hasSecret: boolean
  /** Callback chamado após revogação bem-sucedida (para recarregar o destino pai). */
  onRevoked?: () => void
}

export const CredentialPanel: React.FC<CredentialPanelProps> = ({
  destinationId,
  hasSecret,
  onRevoked,
}) => {
  const [rotateOpen, setRotateOpen] = useState(false)
  const [revokeOpen, setRevokeOpen] = useState(false)
  const [rotating, setRotating] = useState(false)
  const [revoking, setRevoking] = useState(false)
  const [feedback, setFeedback] = useState<{ type: "success" | "error"; message: string } | null>(null)

  // Auditoria de acesso
  const auditLoader = useCallback(
    () => api.getCredentialAudit(destinationId, { limit: 20 }),
    [destinationId],
  )
  const { data: audit, loading: auditLoading, error: auditError, reload: reloadAudit } =
    useAsyncResource<CredentialAuditResponse>(auditLoader)

  const handleRotate = async (req: CredentialRotateRequest) => {
    setRotating(true)
    try {
      await api.rotateCredential(destinationId, req)
      setRotateOpen(false)
      setFeedback({ type: "success", message: "Credencial rotacionada com sucesso. Versão incrementada." })
      reloadAudit()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : "Falha ao rotacionar credencial." })
    } finally {
      setRotating(false)
    }
  }

  const handleRevoke = async () => {
    setRevoking(true)
    try {
      await api.revokeCredential(destinationId)
      setRevokeOpen(false)
      setFeedback({ type: "success", message: "Credencial revogada. O destino foi desabilitado automaticamente." })
      reloadAudit()
      onRevoked?.()
    } catch (err) {
      setFeedback({ type: "error", message: err instanceof Error ? err.message : "Falha ao revogar credencial." })
    } finally {
      setRevoking(false)
    }
  }

  return (
    <div className="space-y-4">
      {/* Feedback */}
      {feedback && (
        <Notice
          variant={feedback.type === "success" ? "success" : "danger"}
          title={feedback.type === "success" ? "Operação concluída" : "Erro"}
        >
          {feedback.message}
        </Notice>
      )}

      {/* Ações de credencial */}
      <Card padding="md" className="space-y-3">
        <div className="flex items-center justify-between">
          <div className="flex items-center gap-2">
            <KeyRoundIcon size={16} className="text-text-tertiary" aria-hidden="true" />
            <h3 className="text-sm font-semibold text-text">Credencial</h3>
            <Badge variant={hasSecret ? "primary" : "default"} size="sm">
              {hasSecret ? "configurada" : "sem credencial"}
            </Badge>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            size="sm"
            leftIcon={<RefreshCcwIcon size={14} />}
            onClick={() => setRotateOpen(true)}
            data-testid="btn-rotate-credential"
          >
            Rotacionar
          </Button>
          <Button
            variant="ghost"
            size="sm"
            leftIcon={<ShieldOffIcon size={14} />}
            onClick={() => setRevokeOpen(true)}
            disabled={!hasSecret}
            data-testid="btn-revoke-credential"
          >
            Revogar
          </Button>
        </div>

        <p className="text-xs text-text-tertiary">
          O segredo nunca é exibido após salvo. Revogar desabilita o destino imediatamente.
        </p>
      </Card>

      {/* Auditoria de acesso */}
      <Card padding="md" className="space-y-3">
        <div className="flex items-center justify-between">
          <h4 className="text-sm font-semibold text-text">Auditoria de acesso</h4>
          <Button
            variant="ghost"
            size="sm"
            onClick={reloadAudit}
            leftIcon={<RefreshCcwIcon size={14} />}
            aria-label="Recarregar auditoria"
          >
            Atualizar
          </Button>
        </div>

        {auditLoading && (
          <div role="status" aria-label="Carregando auditoria…">
            <SkeletonText lines={4} />
          </div>
        )}
        {auditError && !auditLoading && (
          <ErrorState
            title="Falha ao carregar auditoria"
            message={auditError.message}
            onRetry={reloadAudit}
          />
        )}
        {!auditLoading && !auditError && audit && audit.entries.length === 0 && (
          <p className="text-sm text-text-tertiary">Nenhum acesso registrado ainda.</p>
        )}
        {!auditLoading && !auditError && audit && audit.entries.length > 0 && (
          <div>
            <div className="mb-2 text-xs text-text-tertiary">
              {audit.total} registro{audit.total !== 1 ? "s" : ""} (últimos 20)
            </div>
            <div data-testid="audit-entries">
              {audit.entries.map((entry) => (
                <AuditRow key={entry.id} entry={entry} />
              ))}
            </div>
          </div>
        )}
      </Card>

      {/* Modal de rotação */}
      <Modal
        open={rotateOpen}
        onClose={() => !rotating && setRotateOpen(false)}
        title="Rotacionar credencial"
        size="sm"
      >
        <RotateForm
          loading={rotating}
          onSubmit={handleRotate}
          onCancel={() => setRotateOpen(false)}
        />
      </Modal>

      {/* Confirm de revogação */}
      <ConfirmDialog
        open={revokeOpen}
        title="Revogar credencial"
        description="Revogar apaga a credencial e desabilita o destino imediatamente. Eventos novos não serão entregues até que uma nova credencial seja configurada. Esta ação não pode ser desfeita."
        confirmLabel="Revogar"
        confirmVariant="danger"
        loading={revoking}
        onConfirm={handleRevoke}
        onClose={() => setRevokeOpen(false)}
        data-testid="revoke-confirm-dialog"
      />
    </div>
  )
}

export default CredentialPanel
